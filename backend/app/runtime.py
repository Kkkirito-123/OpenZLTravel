"""持久规划会话运行时。

本模块负责后台任务、幂等、恢复、步骤状态、重试和取消。它不解析供应商响应，也不计算
行程规则；会话写锁和任务租约由 Redis 跨 Worker 协调，PostgreSQL 保存权威快照。
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.coordination import Coordination, LocalCoordination
from app.errors import (
    AppError,
    CoordinationUnavailableError,
    ResourceNotFoundError,
    SessionBusyError,
)
from app.models import (
    AccommodationPlan,
    HotelDetail,
    IntercityPlan,
    PlanningRequest,
    PlanningSelection,
    PlanningSession,
    PlanningStep,
    RailOption,
)
from app.providers import HotelProvider, RailProvider
from app.storage import UNSCOPED_VISITOR_ID, PlanningRepository
from app.travel import TravelService
from app.workflow import GenerationState, WorkbenchWorkflow

LOGGER = logging.getLogger("openzltravel.runtime")

STEP_LABELS = {
    "poi": "地点数据",
    "rail_outbound": "去程车票",
    "rail_return": "返程车票",
    "hotels": "住宿",
    "weather": "天气",
    "planning": "日程规划",
    "copy": "文案润色",
    "transport": "市内交通",
    "finalize": "结果组装",
}


class PlanningRuntime:
    """管理可轮询、可恢复的本地旅行规划会话。"""

    def __init__(
        self,
        repository: PlanningRepository,
        travel_service: TravelService,
        workflow: WorkbenchWorkflow,
        rail: RailProvider,
        hotels: HotelProvider,
        coordination: Coordination | None = None,
        recovery_scan_seconds: int = 5,
    ) -> None:
        self.repository = repository
        self.travel_service = travel_service
        self.workflow = workflow
        self.rail = rail
        self.hotels = hotels
        self.coordination = coordination or LocalCoordination()
        self.recovery_scan_seconds = max(1, recovery_scan_seconds)
        self.tasks: dict[tuple[UUID, UUID], asyncio.Task[None]] = {}
        self._session_tasks: dict[tuple[UUID, UUID], set[asyncio.Task[None]]] = {}
        self._recovery_task: asyncio.Task[None] | None = None

    def start(
        self,
        request: PlanningRequest,
        idempotency_key: str | None = None,
        visitor_id: UUID = UNSCOPED_VISITOR_ID,
    ) -> PlanningSession:
        """创建并调度发现任务；本方法不等待任何外部服务。"""

        if idempotency_key:
            cached_id = self.coordination.get_idempotency(visitor_id, idempotency_key)
            if cached_id is not None:
                cached = self.repository.get_session(cached_id, visitor_id)
                if cached is not None:
                    return cached
        now = _now()
        candidate = PlanningSession(
            session_id=uuid4(),
            request=request,
            steps=[PlanningStep(name=name, label=label) for name, label in STEP_LABELS.items()],
            created_at=now,
            updated_at=now,
        )
        session = self.repository.create_session(candidate, idempotency_key, visitor_id)
        if idempotency_key:
            self.coordination.set_idempotency(visitor_id, idempotency_key, session.session_id)
        if session.session_id == candidate.session_id:
            self._schedule(
                visitor_id,
                session.session_id,
                lambda: self._run_discovery(session.session_id, visitor_id),
            )
        return session

    def get(
        self, session_id: UUID, visitor_id: UUID = UNSCOPED_VISITOR_ID
    ) -> PlanningSession:
        """读取规划会话，不存在时返回稳定错误。"""

        session = self.repository.get_session(session_id, visitor_id)
        if session is None:
            raise ResourceNotFoundError("规划会话不存在")
        return session

    async def recover(self) -> None:
        """立即扫描一次，并启动每五秒重复执行的任务接管循环。"""

        await self._recover_once()
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._recovery_loop())

    async def _recover_once(self) -> None:
        """把 PostgreSQL 中的可恢复任务交给 Redis 租约竞争。"""

        for visitor_id, session in self.repository.list_recoverable_sessions():
            session_id = session.session_id
            if session.status == "generating":
                self._schedule(
                    visitor_id,
                    session_id,
                    _task_factory(self._run_generation, session_id, visitor_id),
                )
            else:
                self._schedule(
                    visitor_id,
                    session_id,
                    _task_factory(self._run_discovery, session_id, visitor_id),
                )

    async def _recovery_loop(self) -> None:
        """持续发现崩溃 Worker 留下的任务，租约未过期时不会重复执行。"""

        while True:
            await asyncio.sleep(self.recovery_scan_seconds)
            try:
                await self._recover_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("planning_recovery_scan_failed")

    async def update_selection(
        self,
        session_id: UUID,
        selection: PlanningSelection,
        visitor_id: UUID = UNSCOPED_VISITOR_ID,
    ) -> PlanningSession:
        """校验选择只引用当前会话中的真实候选。"""

        session = self.get(session_id, visitor_id)
        if session.status not in {"awaiting_selection", "failed"}:
            raise AppError("invalid_session_state", "当前阶段不能修改选择", 409)
        _validate_selection(session, selection)
        quoted = await self._quote_selected_transfers(session, selection)
        async with self.coordination.session_lock(session_id):
            current = self.get(session_id, visitor_id)
            if current.status not in {"awaiting_selection", "failed"}:
                raise AppError("invalid_session_state", "当前阶段不能修改选择", 409)
            _validate_selection(current, selection)
            updated = current.model_copy(
                update={"selection": selection, "updated_at": _now(), **quoted}
            )
            self.repository.save_session(updated, visitor_id)
            return updated

    async def _quote_selected_transfers(
        self, session: PlanningSession, selection: PlanningSelection
    ) -> dict[str, list[RailOption]]:
        updates: dict[str, list[RailOption]] = {}
        pairs = (
            ("outbound_transfers", session.outbound_transfers, selection.outbound),
            ("return_transfers", session.return_transfers, selection.return_trip),
        )
        for field, options, choice in pairs:
            option = _find_option(options, choice.option_id) if choice else None
            if option is None or not option.is_transfer or option.price_from is not None:
                continue
            try:
                quoted = await self.rail.quote_transfer(option)
            except AppError:
                continue
            updates[field] = [
                quoted if item.option_id == quoted.option_id else item for item in options
            ]
        return updates

    async def search_transfers(
        self,
        session_id: UUID,
        direction: str,
        visitor_id: UUID = UNSCOPED_VISITOR_ID,
    ) -> list[RailOption]:
        """按需查询中转方案并保存到会话。"""

        session = self.get(session_id, visitor_id)
        if session.status != "awaiting_selection":
            raise AppError("invalid_session_state", "当前阶段不能查询中转车次", 409)
        request = session.request
        if direction == "outbound":
            origin, destination, travel_date = (
                request.origin,
                request.destination,
                request.start_date,
            )
        else:
            origin, destination, travel_date = (
                request.destination,
                request.origin,
                request.end_date,
            )
        options, _ = await self.rail.transfers(origin, destination, travel_date, direction)
        async with self.coordination.session_lock(session_id):
            current = self.get(session_id, visitor_id)
            if current.status != "awaiting_selection":
                raise AppError("invalid_session_state", "当前阶段不能查询中转车次", 409)
            field = "outbound_transfers" if direction == "outbound" else "return_transfers"
            updated = current.model_copy(update={field: options, "updated_at": _now()})
            self.repository.save_session(updated, visitor_id)
        return options

    async def hotel_detail(
        self,
        session_id: UUID,
        hotel_id: str,
        visitor_id: UUID = UNSCOPED_VISITOR_ID,
    ) -> HotelDetail:
        """按点击查询房型和退改规则，不扩大首次发现耗时。"""

        session = self.get(session_id, visitor_id)
        hotel = next((item for item in session.hotel_options if item.hotel_id == hotel_id), None)
        if hotel is None:
            raise AppError("hotel_not_found", "酒店候选不存在", 404)
        detail, _ = await self.hotels.detail(hotel, session.request)
        return detail

    async def generate(
        self, session_id: UUID, visitor_id: UUID = UNSCOPED_VISITOR_ID
    ) -> PlanningSession:
        """确认选择后调度确定性生成并立即返回会话。"""

        async with self.coordination.session_lock(session_id):
            session = self.get(session_id, visitor_id)
            if session.status in {"generating", "completed"}:
                return session
            if session.status != "awaiting_selection":
                raise AppError("invalid_session_state", "当前阶段不能生成行程", 409)
            _require_complete_selection(session)
            updated = session.model_copy(
                update={
                    "status": "generating",
                    "error_code": None,
                    "error_message": None,
                    "updated_at": _now(),
                }
            )
            self.repository.save_session(updated, visitor_id)
        self._schedule(
            visitor_id,
            session_id,
            lambda: self._run_generation(session_id, visitor_id),
            queue_if_busy=True,
        )
        return updated

    async def retry(
        self, session_id: UUID, visitor_id: UUID = UNSCOPED_VISITOR_ID
    ) -> PlanningSession:
        """重试失败会话；已有完整发现数据时只重跑生成。"""

        async with self.coordination.session_lock(session_id):
            session = self.get(session_id, visitor_id)
            if session.status not in {"failed", "awaiting_selection"}:
                raise AppError("invalid_session_state", "当前会话无需重试", 409)
            generate = bool(session.candidates and _selection_complete(session))
            status = "generating" if generate else "searching"
            updated = session.model_copy(
                update={
                    "status": status,
                    "error_code": None,
                    "error_message": None,
                    "updated_at": _now(),
                }
            )
            self.repository.save_session(updated, visitor_id)
        if generate:
            self._schedule(
                visitor_id,
                session_id,
                lambda: self._run_generation(session_id, visitor_id),
                queue_if_busy=True,
            )
        else:
            self._schedule(
                visitor_id,
                session_id,
                lambda: self._run_discovery(session_id, visitor_id),
                queue_if_busy=True,
            )
        return updated

    async def cancel(
        self, session_id: UUID, visitor_id: UUID = UNSCOPED_VISITOR_ID
    ) -> None:
        """取消未完成会话，并先持久化终态以阻止迟到任务覆盖。"""

        async with self.coordination.session_lock(session_id):
            session = self.get(session_id, visitor_id)
            if session.status == "cancelled":
                return
            if session.status == "completed":
                raise AppError("invalid_session_state", "已完成的行程不能取消", 409)
            self.repository.save_session(
                session.model_copy(update={"status": "cancelled", "updated_at": _now()}),
                visitor_id,
            )
        await self._cancel_session_tasks(session_id, visitor_id)

    async def close(self) -> None:
        """停止并等待全部后台任务，为进程关闭释放运行时状态。"""

        if self._recovery_task is not None:
            self._recovery_task.cancel()
            await asyncio.gather(self._recovery_task, return_exceptions=True)
            self._recovery_task = None
        tasks = {
            task
            for session_tasks in self._session_tasks.values()
            for task in session_tasks
            if not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self._session_tasks.clear()

    async def _run_discovery(self, session_id: UUID, visitor_id: UUID) -> None:
        try:
            session = self.get(session_id, visitor_id)
            result = await self.workflow.discover(
                session.request,
                self._step_callback(session_id, visitor_id),
            )
            warnings = _unique(
                [
                    *result.get("outbound_warnings", []),
                    *result.get("return_warnings", []),
                    *result.get("hotel_warnings", []),
                    *result.get("weather_warnings", []),
                ]
            )
            await self._set_session(
                session_id,
                visitor_id,
                status="awaiting_selection",
                city=result.get("city"),
                candidates=result.get("candidates"),
                outbound_options=result.get("outbound_options", []),
                return_options=result.get("return_options", []),
                outbound_transfers=result.get("outbound_transfers", []),
                return_transfers=result.get("return_transfers", []),
                hotel_options=result.get("hotel_options", []),
                weather=result.get("weather", []),
                warnings=warnings,
                error_code=None,
                error_message=None,
            )
        except asyncio.CancelledError:
            return
        except (CoordinationUnavailableError, SessionBusyError):
            # 锁争用是可恢复的基础设施状态，不应把完整规划误标为业务失败。
            LOGGER.warning("planning_discovery_paused session_id=%s", session_id)
            return
        except Exception as error:
            await self._fail(session_id, visitor_id, error)

    async def _run_generation(self, session_id: UUID, visitor_id: UUID) -> None:
        try:
            session = self.get(session_id, visitor_id)
            existing = self.travel_service.repository.get(session_id, visitor_id)
            if existing and existing.planning_session_id == session_id:
                await self._set_session(
                    session_id,
                    visitor_id,
                    status="completed",
                    trip_id=existing.trip_id,
                )
                return
            state = _generation_state(session, self._step_callback(session_id, visitor_id))
            itinerary = await self.workflow.generate(state)
            # 完整结构通过图中校验后才保存；失败时历史列表不会出现半成品。
            self.travel_service.repository.save(itinerary, session.request, visitor_id)
            await self._set_session(
                session_id,
                visitor_id,
                status="completed",
                trip_id=itinerary.trip_id,
                error_code=None,
                error_message=None,
            )
        except asyncio.CancelledError:
            return
        except (CoordinationUnavailableError, SessionBusyError):
            LOGGER.warning("planning_generation_paused session_id=%s", session_id)
            return
        except Exception as error:
            await self._fail(session_id, visitor_id, error)

    def _step_callback(self, session_id: UUID, visitor_id: UUID) -> Callable[..., Any]:
        async def callback(name: str, status: str, **values: Any) -> None:
            async with self.coordination.session_lock(session_id):
                session = self.get(session_id, visitor_id)
                steps = [_updated_step(item, name, status, values) for item in session.steps]
                LOGGER.info(
                    "planning_step session_id=%s step=%s status=%s duration_ms=%s cache_hit=%s",
                    session_id,
                    name,
                    status,
                    values.get("duration_ms"),
                    values.get("cache_hit", False),
                )
                self.repository.save_session(
                    session.model_copy(update={"steps": steps, "updated_at": _now()}),
                    visitor_id,
                )

        return callback

    async def _set_session(
        self, session_id: UUID, visitor_id: UUID, **values: Any
    ) -> PlanningSession:
        async with self.coordination.session_lock(session_id):
            session = self.get(session_id, visitor_id)
            # 取消是终态。迟到的发现或生成任务只能结束，不能把会话重新推进。
            if session.status == "cancelled" and values.get("status") != "cancelled":
                return session
            updated = session.model_copy(update={**values, "updated_at": _now()})
            self.repository.save_session(updated, visitor_id)
            return updated

    async def _fail(self, session_id: UUID, visitor_id: UUID, error: Exception) -> None:
        code = error.code if isinstance(error, AppError) else "planning_failed"
        message = error.message if isinstance(error, AppError) else "规划任务执行失败，请重试"
        LOGGER.exception("planning_session_failed session_id=%s code=%s", session_id, code)
        await self._set_session(
            session_id,
            visitor_id,
            status="failed",
            error_code=code,
            error_message=message,
        )

    def _schedule(
        self,
        visitor_id: UUID,
        session_id: UUID,
        operation_factory: Callable[[], Coroutine[Any, Any, None]],
        queue_if_busy: bool = False,
    ) -> None:
        key = (visitor_id, session_id)
        existing = self.tasks.get(key)
        if existing and not existing.done():
            if not queue_if_busy:
                return

            async def after_existing() -> None:
                await asyncio.gather(existing, return_exceptions=True)
                await self._run_with_lease(session_id, operation_factory)

            task = asyncio.create_task(after_existing())
        else:
            task = asyncio.create_task(self._run_with_lease(session_id, operation_factory))
        self.tasks[key] = task
        self._session_tasks.setdefault(key, set()).add(task)
        task.add_done_callback(
            lambda completed: self._forget_task(session_id, visitor_id, completed)
        )

    def _forget_task(
        self,
        session_id: UUID,
        visitor_id: UUID,
        completed: asyncio.Task[Any],
    ) -> None:
        key = (visitor_id, session_id)
        session_tasks = self._session_tasks.get(key)
        if session_tasks is not None:
            session_tasks.discard(completed)
            if not session_tasks:
                self._session_tasks.pop(key, None)
        if self.tasks.get(key) is completed:
            self.tasks.pop(key, None)
        if not completed.cancelled():
            with contextlib.suppress(Exception):
                completed.exception()

    async def _run_with_lease(
        self,
        session_id: UUID,
        operation_factory: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """只有租约持有者执行；续租丢失时取消任务并等待其他 Worker 接管。"""

        try:
            async with self.coordination.task_lease(session_id) as lease:
                if lease is None:
                    return
                operation = asyncio.create_task(operation_factory())
                lost = asyncio.create_task(lease.wait_lost())
                try:
                    done, _ = await asyncio.wait(
                        {operation, lost}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if lost in done and not operation.done():
                        LOGGER.warning("planning_task_lease_lost session_id=%s", session_id)
                        # 租约已交给其他 Worker，旧 Worker 必须停止写入，避免覆盖新状态。
                        return
                    await operation
                finally:
                    for task in (operation, lost):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(operation, lost, return_exceptions=True)
        except CoordinationUnavailableError:
            LOGGER.warning("planning_task_lease_unavailable session_id=%s", session_id)

    async def _cancel_session_tasks(self, session_id: UUID, visitor_id: UUID) -> None:
        """取消同一会话的任务链，避免前置任务在取消后覆盖最终状态。"""

        tasks = tuple(
            task
            for task in self._session_tasks.get((visitor_id, session_id), set())
            if not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _task_factory(
    runner: Callable[[UUID, UUID], Coroutine[Any, Any, None]],
    session_id: UUID,
    visitor_id: UUID,
) -> Callable[[], Coroutine[Any, Any, None]]:
    """延迟创建恢复协程，避免任务被取消前留下未等待的 coroutine。"""

    def operation() -> Coroutine[Any, Any, None]:
        return runner(session_id, visitor_id)

    return operation


def _updated_step(
    step: PlanningStep, name: str, status: str, values: dict[str, Any]
) -> PlanningStep:
    if step.name != name:
        return step
    attempts = step.attempts + 1 if status == "running" else step.attempts
    allowed = {key: value for key, value in values.items() if key in PlanningStep.model_fields}
    return step.model_copy(update={"status": status, "attempts": attempts, **allowed})


def _validate_selection(session: PlanningSession, selection: PlanningSelection) -> None:
    outbound = [*session.outbound_options, *session.outbound_transfers]
    returns = [*session.return_options, *session.return_transfers]
    if selection.outbound and not _find_option(outbound, selection.outbound.option_id):
        raise AppError("invalid_selection", "去程车次不在当前候选中", 422)
    if selection.return_trip and not _find_option(returns, selection.return_trip.option_id):
        raise AppError("invalid_selection", "返程车次不在当前候选中", 422)
    if selection.hotel_id and not any(
        item.hotel_id == selection.hotel_id for item in session.hotel_options
    ):
        raise AppError("invalid_selection", "酒店不在当前候选中", 422)
    if selection.outbound:
        _validate_seat(_find_option(outbound, selection.outbound.option_id), selection.outbound)
    if selection.return_trip:
        _validate_seat(
            _find_option(returns, selection.return_trip.option_id),
            selection.return_trip,
        )


def _require_complete_selection(session: PlanningSession) -> None:
    if not _selection_complete(session):
        raise AppError("selection_required", "请选择往返车次和住宿，或标记为自行安排", 422)


def _selection_complete(session: PlanningSession) -> bool:
    selection = session.selection
    rail_ready = bool(selection.outbound or selection.self_arranged_outbound) and bool(
        selection.return_trip or selection.self_arranged_return
    )
    hotel_ready = session.request.days_count == 1 or bool(
        selection.hotel_id or selection.self_arranged_hotel
    )
    return rail_ready and hotel_ready


def _generation_state(session: PlanningSession, callback: Any) -> GenerationState:
    if session.city is None or session.candidates is None:
        raise AppError("session_data_missing", "规划会话缺少地点数据，请重试", 409)
    outbound = _selected_option(
        [*session.outbound_options, *session.outbound_transfers],
        session.selection.outbound,
    )
    return_trip = _selected_option(
        [*session.return_options, *session.return_transfers],
        session.selection.return_trip,
    )
    hotel = next(
        (item for item in session.hotel_options if item.hotel_id == session.selection.hotel_id),
        None,
    )
    warnings = [*session.warnings, *_price_warnings(outbound, return_trip, hotel)]
    return GenerationState(
        session_id=session.session_id,
        request=session.request,
        city=session.city,
        candidates=session.candidates,
        weather=session.weather,
        intercity=IntercityPlan(
            outbound=outbound,
            return_trip=return_trip,
            self_arranged_outbound=session.selection.self_arranged_outbound,
            self_arranged_return=session.selection.self_arranged_return,
        ),
        accommodation=AccommodationPlan(
            hotel=hotel,
            check_in=session.request.start_date,
            check_out=session.request.end_date,
            nights=max(0, (session.request.end_date - session.request.start_date).days),
            self_arranged=(
                session.selection.self_arranged_hotel or session.request.days_count == 1
            ),
        ),
        warnings=_unique(warnings),
        on_step=callback,
    )


def _selected_option(options: list[RailOption], choice: Any) -> RailOption | None:
    if choice is None:
        return None
    option = _find_option(options, choice.option_id)
    if option is None or not choice.seat_type:
        return option
    seat = next((item for item in option.seats if item.name == choice.seat_type), None)
    return option.model_copy(update={"price_from": seat.price}) if seat else option


def _find_option(options: list[RailOption], option_id: str) -> RailOption | None:
    return next((item for item in options if item.option_id == option_id), None)


def _validate_seat(option: RailOption | None, choice: Any) -> None:
    if option is None or choice is None or not choice.seat_type:
        return
    if not any(item.name == choice.seat_type for item in option.seats):
        raise AppError("invalid_selection", "所选席别不在当前车次中", 422)


def _price_warnings(
    outbound: RailOption | None, return_trip: RailOption | None, hotel: Any
) -> list[str]:
    warnings: list[str] = []
    if outbound and outbound.price_from is None:
        warnings.append("去程票价未知，未计入预算。")
    if return_trip and return_trip.price_from is None:
        warnings.append("返程票价未知，未计入预算。")
    if hotel and hotel.price_per_night is None and hotel.total_price is None:
        warnings.append("酒店价格未知，未计入预算。")
    return warnings


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _now() -> datetime:
    return datetime.now(timezone.utc)
