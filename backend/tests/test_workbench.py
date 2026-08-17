import asyncio
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.errors import AppError, ConflictError, DraftError, ProviderError
from app.models import (
    City,
    DayActivityEdit,
    DayEditRequest,
    HotelDetail,
    HotelOption,
    PlanningRequest,
    PlanningSelection,
    PlanningSession,
    RailChoice,
    RailOption,
    RailSeat,
    RailSegment,
)
from app.providers import (
    DeterministicPlanner,
    HotelProvider,
    McpHttpClient,
    ProviderExecutor,
    RollingGoHotelClient,
    TransportResult,
)
from app.providers.base import _response_payload, _tool_result
from app.providers.maps import local_routes
from app.providers.rail import RailProvider
from app.runtime import PlanningRuntime
from app.travel import TravelService
from app.workflow import WorkbenchWorkflow, _fetch_transport
from tests.fakes import FakeMapProvider, FakePlanner
from tests.sqlite_repository import SqliteTripRepository


class AsyncMapProvider(FakeMapProvider):
    """为工作台提供异步交通能力的离线地图替身。"""

    async def get_transport_async(self, city, pois, mode):
        return TransportResult(local_routes(pois, mode), [])


class FakeRailProvider:
    """返回固定往返车次，不访问真实 12306。"""

    async def search(self, origin, destination, travel_date, direction):
        await asyncio.sleep(0.01)
        return [rail_option(origin, destination, travel_date, direction)], False

    async def transfers(self, origin, destination, travel_date, direction):
        return [], False


class FakeHotelProvider:
    """返回固定酒店及详情。"""

    def __init__(self) -> None:
        self.search_calls = 0

    async def search(self, request, catalog):
        self.search_calls += 1
        hotel = catalog.hotels[0]
        return (
            [
                HotelOption(
                    hotel_id=hotel.id,
                    name=hotel.name,
                    address=hotel.address,
                    latitude=hotel.latitude,
                    longitude=hotel.longitude,
                    price_per_night=300,
                    total_price=300,
                    source="osm",
                )
            ],
            False,
            None,
        )

    async def detail(self, hotel, request):
        return HotelDetail(
            hotel_id=hotel.hotel_id,
            name=hotel.name,
            address=hotel.address,
            source="osm",
        ), False


class FakeMcpClient:
    """模拟 12306 两个只读工具的 JSON 结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "search-stations":
            return {
                "success": True,
                "stations": [{"name": arguments["query"], "code": "TST"}],
            }
        if name == "query-tickets":
            return {
                "trains": [
                    {
                        "train_no": "G1234",
                        "from_station": "北京南",
                        "to_station": "杭州东",
                        "start_time": "08:00",
                        "arrive_time": "12:30",
                        "duration": "04:30",
                        "seats": {"second_class": "有", "first_class": "无"},
                    }
                ]
            }
        return {
            "data": [
                {
                    "train_code": "G1234",
                    "prices": {"二等座": "538.5", "一等座": "907.0"},
                }
            ]
        }


class FailedMcpClient:
    """模拟已经映射成稳定错误的 MCP 失败。"""

    async def call_tool(self, name, arguments):
        del name, arguments
        from app.errors import ProviderError

        raise ProviderError("mcp_unavailable", "测试上游不可用")


class FailingCopyEnhancer:
    """模拟文案增强超时或返回非法结构。"""

    enabled = True
    settings = SimpleNamespace(llm_enhancement_timeout_seconds=0.01)

    def __init__(self, mode: str) -> None:
        self.mode = mode

    def enhance(self, request, draft):
        del request
        if self.mode == "timeout":
            time.sleep(0.05)
            return draft.model_copy(update={"summary": "不应采用的迟到结果"})
        raise DraftError("测试非法文案")


class CountingRepository(SqliteTripRepository):
    """记录最终行程写入次数，验证恢复与并发不会重复保存。"""

    def __init__(self, database_path: str) -> None:
        super().__init__(database_path)
        self.save_calls = 0

    def save(self, itinerary, request, visitor_id=None):
        del visitor_id
        self.save_calls += 1
        super().save(itinerary, request)


def planning_request() -> PlanningRequest:
    return PlanningRequest(
        origin="北京",
        destination="测试市",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 2),
        travelers=2,
        budget=5000,
    )


def rail_option(origin, destination, travel_date, direction) -> RailOption:
    return RailOption(
        option_id=f"{direction}-g1",
        direction=direction,
        travel_date=travel_date,
        train_code="G1",
        train_type="高铁",
        from_station=origin,
        to_station=destination,
        departure_time="08:00",
        arrival_time="12:00",
        duration_minutes=240,
        seats=[
            RailSeat(name="二等座", availability="有", price=200),
            RailSeat(name="一等座", availability="有", price=500),
        ],
        price_from=200,
        has_ticket=True,
    )


def make_runtime(tmp_path: Path):
    repository = SqliteTripRepository(str(tmp_path / "workbench.sqlite3"))
    map_provider = AsyncMapProvider()
    service = TravelService(map_provider, FakePlanner(), repository)
    rail = FakeRailProvider()
    hotels = FakeHotelProvider()
    workflow = WorkbenchWorkflow(service, rail, hotels, DeterministicPlanner())
    runtime = PlanningRuntime(repository, service, workflow, rail, hotels)
    return runtime, repository


async def wait_for(runtime: PlanningRuntime, session_id, status: str) -> PlanningSession:
    for _ in range(100):
        session = runtime.get(session_id)
        if session.status == status:
            return session
        if session.status == "failed":
            pytest.fail(session.error_message or "规划会话失败")
        await asyncio.sleep(0.01)
    pytest.fail(f"会话未进入 {status}")


@pytest.mark.asyncio
async def test_session_is_idempotent_and_saves_one_complete_trip(tmp_path: Path) -> None:
    runtime, repository = make_runtime(tmp_path)

    first = runtime.start(planning_request(), "same-request")
    duplicate = runtime.start(planning_request(), "same-request")

    assert first.session_id == duplicate.session_id
    discovered = await wait_for(runtime, first.session_id, "awaiting_selection")
    assert discovered.outbound_options and discovered.return_options
    assert discovered.hotel_options
    assert all(step.status in {"completed", "pending"} for step in discovered.steps)

    await runtime.update_selection(
        first.session_id,
        PlanningSelection(
            outbound=RailChoice(option_id=discovered.outbound_options[0].option_id),
            return_trip=RailChoice(option_id=discovered.return_options[0].option_id),
            hotel_id=discovered.hotel_options[0].hotel_id,
        ),
    )
    await runtime.generate(first.session_id)
    completed = await wait_for(runtime, first.session_id, "completed")

    assert completed.trip_id == first.session_id
    assert len(repository.list()) == 1
    itinerary = repository.get(first.session_id)
    assert itinerary and itinerary.planning_session_id == first.session_id
    assert itinerary.intercity and itinerary.intercity.outbound
    assert itinerary.accommodation and itinerary.accommodation.hotel
    assert itinerary.budget.total == sum(day.budget.total for day in itinerary.days if day.budget)


@pytest.mark.asyncio
async def test_provider_executor_keeps_shared_query_when_owner_is_cancelled(
    tmp_path: Path,
) -> None:
    """页面断开不应中断其他请求正在等待的同一供应商调用。"""

    repository = SqliteTripRepository(str(tmp_path / "shared-query.sqlite3"))
    executor = ProviderExecutor("rail", repository)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "ok"

    owner = asyncio.create_task(executor.run("same", 60, operation))
    await started.wait()
    follower = asyncio.create_task(executor.run("same", 60, operation))
    await asyncio.sleep(0)

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert "same" in executor.inflight

    release.set()
    assert await follower == ("ok", False)
    await asyncio.sleep(0)
    assert calls == 1
    assert executor.inflight == {}


@pytest.mark.asyncio
async def test_selected_seat_price_enters_intercity_budget(tmp_path: Path) -> None:
    runtime, repository = make_runtime(tmp_path)
    session = runtime.start(planning_request())
    discovered = await wait_for(runtime, session.session_id, "awaiting_selection")

    await runtime.update_selection(
        session.session_id,
        PlanningSelection(
            outbound=RailChoice(
                option_id=discovered.outbound_options[0].option_id,
                seat_type="一等座",
            ),
            return_trip=RailChoice(
                option_id=discovered.return_options[0].option_id,
                seat_type="一等座",
            ),
            hotel_id=discovered.hotel_options[0].hotel_id,
        ),
    )
    await runtime.generate(session.session_id)
    await wait_for(runtime, session.session_id, "completed")

    itinerary = repository.get(session.session_id)
    assert itinerary and itinerary.intercity
    assert itinerary.intercity.outbound and itinerary.intercity.outbound.price_from == 500
    assert itinerary.intercity.return_trip and itinerary.intercity.return_trip.price_from == 500
    assert itinerary.budget.intercity_transport == 2000


@pytest.mark.asyncio
async def test_selected_costs_are_checked_against_budget_after_aggregation(
    tmp_path: Path,
) -> None:
    """真实车票报价进入总额后，预算超限提示必须基于最终金额。"""

    runtime, repository = make_runtime(tmp_path)
    request = planning_request().model_copy(update={"budget": 2500})
    session = runtime.start(request)
    discovered = await wait_for(runtime, session.session_id, "awaiting_selection")

    await runtime.update_selection(
        session.session_id,
        PlanningSelection(
            outbound=RailChoice(
                option_id=discovered.outbound_options[0].option_id,
                seat_type="一等座",
            ),
            return_trip=RailChoice(
                option_id=discovered.return_options[0].option_id,
                seat_type="一等座",
            ),
            hotel_id=discovered.hotel_options[0].hotel_id,
        ),
    )
    await runtime.generate(session.session_id)
    await wait_for(runtime, session.session_id, "completed")

    itinerary = repository.get(session.session_id)
    assert itinerary and itinerary.budget.total > request.budget
    assert any("高于你的预算上限" in warning for warning in itinerary.warnings)


@pytest.mark.asyncio
async def test_one_day_trip_does_not_require_hotel_selection(tmp_path: Path) -> None:
    runtime, repository = make_runtime(tmp_path)
    request = planning_request().model_copy(update={"end_date": date(2026, 9, 1)})
    session = runtime.start(request)
    discovered = await wait_for(runtime, session.session_id, "awaiting_selection")

    await runtime.update_selection(
        session.session_id,
        PlanningSelection(
            outbound=RailChoice(option_id=discovered.outbound_options[0].option_id),
            return_trip=RailChoice(option_id=discovered.return_options[0].option_id),
        ),
    )
    await runtime.generate(session.session_id)
    await wait_for(runtime, session.session_id, "completed")

    itinerary = repository.get(session.session_id)
    assert itinerary and itinerary.accommodation
    assert itinerary.accommodation.nights == 0
    assert itinerary.accommodation.hotel is None
    assert itinerary.budget.hotel == 0
    assert runtime.hotels.search_calls == 0


def test_planner_allows_empty_day_when_rail_uses_the_whole_day() -> None:
    planner = DeterministicPlanner()
    request = planning_request().model_copy(update={"end_date": date(2026, 9, 1)})
    outbound = rail_option("北京", "测试市", request.start_date, "outbound").model_copy(
        update={"arrival_time": "19:00"}
    )
    return_trip = rail_option("测试市", "北京", request.end_date, "return").model_copy(
        update={"departure_time": "09:00"}
    )

    draft = planner.plan(
        request,
        FakeMapProvider().candidates,
        outbound,
        return_trip,
        None,
    )

    assert draft.days[0].activities == []
    assert draft.days[0].theme == "抵达与安顿"


@pytest.mark.asyncio
async def test_failed_weather_is_degraded_instead_of_failing_session(tmp_path: Path) -> None:
    class FailedWeatherMap(AsyncMapProvider):
        async def get_weather_async(self, *args):
            from app.errors import ProviderError

            raise ProviderError("weather_unavailable", "天气失败")

    runtime, _ = make_runtime(tmp_path)
    runtime.travel_service.map_provider = FailedWeatherMap()
    runtime.workflow.service.map_provider = runtime.travel_service.map_provider

    session = runtime.start(planning_request())
    discovered = await wait_for(runtime, session.session_id, "awaiting_selection")

    weather_step = next(item for item in discovered.steps if item.name == "weather")
    assert weather_step.status == "degraded"
    assert len(discovered.weather) == 2
    assert all(item.warning == "暂无预报" for item in discovered.weather)


@pytest.mark.asyncio
async def test_failed_transport_is_degraded_per_day() -> None:
    """一日交通查询失败只能影响该日路线，不能让完整行程生成失败。"""

    class FailedTransportMap:
        async def get_transport_async(self, city, pois, mode):
            del city, pois, mode
            raise ProviderError("route_not_found", "路线服务暂时不可用")

    pois = FakeMapProvider().candidates.attractions[:2]
    result = await _fetch_transport(
        FailedTransportMap(),
        City(name="测试市"),
        {1: pois},
        "transit",
    )

    assert result[1].routes[0].mode == "步行估算"
    assert result[1].warnings == ["公交路线暂时不可用，当前显示为本地步行估算。"]


@pytest.mark.asyncio
async def test_search_transfers_requires_selection_stage(tmp_path: Path) -> None:
    """发现尚未完成时不能写入中转结果，避免被后台发现快照覆盖。"""

    runtime, _ = make_runtime(tmp_path)
    session = runtime.start(planning_request())

    with pytest.raises(AppError, match="不能查询中转"):
        await runtime.search_transfers(session.session_id, "outbound")

    await runtime.cancel(session.session_id)
    assert runtime.get(session.session_id).status == "cancelled"


@pytest.mark.asyncio
async def test_completed_session_cannot_be_cancelled(tmp_path: Path) -> None:
    """取消接口只能终止未完成会话，不能改写已保存行程的会话状态。"""

    runtime, _ = make_runtime(tmp_path)
    session = runtime.start(planning_request())
    await wait_for(runtime, session.session_id, "awaiting_selection")
    await runtime.update_selection(
        session.session_id,
        PlanningSelection(
            self_arranged_outbound=True,
            self_arranged_return=True,
            self_arranged_hotel=True,
        ),
    )
    await runtime.generate(session.session_id)
    await wait_for(runtime, session.session_id, "completed")

    with pytest.raises(AppError, match="不能取消"):
        await runtime.cancel(session.session_id)

    assert runtime.get(session.session_id).status == "completed"


@pytest.mark.asyncio
async def test_revision_conflict_prevents_stale_edit(tmp_path: Path) -> None:
    runtime, repository = make_runtime(tmp_path)
    session = runtime.start(planning_request())
    discovered = await wait_for(runtime, session.session_id, "awaiting_selection")
    await runtime.update_selection(
        session.session_id,
        PlanningSelection(
            self_arranged_outbound=True,
            self_arranged_return=True,
            self_arranged_hotel=True,
        ),
    )
    await runtime.generate(session.session_id)
    await wait_for(runtime, session.session_id, "completed")
    itinerary = repository.get(session.session_id)
    assert itinerary
    activity = itinerary.days[0].activities[0]
    edit = DayEditRequest(
        expected_revision=1,
        activities=[
            DayActivityEdit(
                poi_id=activity.poi_id,
                start_time="10:00",
                duration_minutes=90,
            )
        ],
    )

    updated = await runtime.travel_service.edit_day(
        itinerary.trip_id, 1, edit, discovered.candidates
    )
    assert updated.revision == 2
    with pytest.raises(ConflictError):
        await runtime.travel_service.edit_day(itinerary.trip_id, 1, edit, discovered.candidates)


@pytest.mark.asyncio
async def test_concurrent_edits_commit_only_one_matching_revision(tmp_path: Path) -> None:
    """两个浏览器同时提交同一版本时，SQLite 只能接受一个编辑。"""

    class BlockingEditMap(AsyncMapProvider):
        def __init__(self) -> None:
            super().__init__()
            self.ready = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def get_transport_async(self, city, pois, mode):
            self.calls += 1
            if self.calls == 2:
                self.ready.set()
            await self.release.wait()
            return TransportResult(local_routes(pois, mode), [])

    runtime, repository = make_runtime(tmp_path)
    session = runtime.start(planning_request())
    discovered = await wait_for(runtime, session.session_id, "awaiting_selection")
    await runtime.update_selection(
        session.session_id,
        PlanningSelection(
            self_arranged_outbound=True,
            self_arranged_return=True,
            self_arranged_hotel=True,
        ),
    )
    await runtime.generate(session.session_id)
    await wait_for(runtime, session.session_id, "completed")
    itinerary = repository.get(session.session_id)
    assert itinerary
    activity = itinerary.days[0].activities[0]
    runtime.travel_service.map_provider = BlockingEditMap()

    def edit(start_time: str) -> DayEditRequest:
        return DayEditRequest(
            expected_revision=1,
            activities=[
                DayActivityEdit(
                    poi_id=activity.poi_id,
                    start_time=start_time,
                    duration_minutes=90,
                )
            ],
        )

    first = asyncio.create_task(
        runtime.travel_service.edit_day(itinerary.trip_id, 1, edit("10:00"), discovered.candidates)
    )
    second = asyncio.create_task(
        runtime.travel_service.edit_day(itinerary.trip_id, 1, edit("11:00"), discovered.candidates)
    )
    blocking_map = runtime.travel_service.map_provider
    assert isinstance(blocking_map, BlockingEditMap)
    await blocking_map.ready.wait()
    blocking_map.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert sum(not isinstance(item, BaseException) for item in results) == 1
    assert sum(isinstance(item, ConflictError) for item in results) == 1
    stored = repository.get(itinerary.trip_id)
    assert stored and stored.revision == 2


@pytest.mark.asyncio
async def test_runtime_close_cancels_all_pending_session_tasks(tmp_path: Path) -> None:
    """关闭应用时不遗留发现任务，也不把半成品标记为失败。"""

    class BlockingRailProvider(FakeRailProvider):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def search(self, origin, destination, travel_date, direction):
            self.started.set()
            await self.release.wait()
            return await super().search(origin, destination, travel_date, direction)

    runtime, repository = make_runtime(tmp_path)
    rail = BlockingRailProvider()
    runtime.rail = rail
    runtime.workflow.rail = rail
    session = runtime.start(planning_request())
    await rail.started.wait()

    await runtime.close()

    assert runtime.tasks == {}
    assert runtime._session_tasks == {}
    assert repository.get_session(session.session_id).status == "searching"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_discovery_emits_one_terminal_event_for_each_successful_step(
    tmp_path: Path,
) -> None:
    """步骤元数据应在唯一完成事件写入，不重复污染状态与日志。"""

    runtime, _ = make_runtime(tmp_path)
    events: list[tuple[str, str]] = []

    async def on_step(name: str, status: str, **values) -> None:
        del values
        events.append((name, status))

    await runtime.workflow.discover(planning_request(), on_step)

    for step in ("rail_outbound", "rail_return", "hotels"):
        assert events.count((step, "completed")) == 1


@pytest.mark.asyncio
async def test_rail_provider_merges_availability_and_prices(tmp_path: Path) -> None:
    repository = SqliteTripRepository(str(tmp_path / "cache.sqlite3"))
    client = FakeMcpClient()
    provider = RailProvider(client, ProviderExecutor("rail", repository))

    options, cache_hit = await provider.search("北京南", "杭州东", date(2026, 9, 1), "outbound")

    assert cache_hit is False
    assert options[0].duration_minutes == 270
    assert options[0].price_from == 538.5
    assert options[0].has_ticket is True
    ticket_call = next(arguments for name, arguments in client.calls if name == "query-tickets")
    assert ticket_call["from_station"] == "TST"
    assert ticket_call["to_station"] == "TST"
    _, second_hit = await provider.search("北京南", "杭州东", date(2026, 9, 1), "outbound")
    assert second_hit is True


@pytest.mark.asyncio
async def test_rail_provider_surfaces_mcp_business_errors(tmp_path: Path) -> None:
    class FailedTicketClient:
        async def call_tool(self, name, arguments):
            if name == "search-stations":
                return {"success": True, "stations": [{"code": "TST"}]}
            if name == "query-tickets":
                return {"success": False, "error": "车票查询日期超出预售范围"}
            return {"data": []}

    repository = SqliteTripRepository(str(tmp_path / "rail-error.sqlite3"))
    provider = RailProvider(FailedTicketClient(), ProviderExecutor("rail", repository))

    with pytest.raises(ProviderError, match="超出预售范围"):
        await provider.search("北京", "杭州", date(2026, 9, 1), "outbound")


@pytest.mark.asyncio
async def test_rail_provider_ignores_malformed_transfer_segments(tmp_path: Path) -> None:
    """中转 MCP 混入非对象段时跳过该方案，不让解析异常中断整个发现阶段。"""

    class MalformedTransferClient:
        async def call_tool(self, name, arguments):
            if name == "search-stations":
                return {"stations": [{"code": "TST"}]}
            if name == "query-transfer":
                return {
                    "transfers": [
                        {"segments": [None, "bad"]},
                        {"segments": None},
                    ]
                }
            raise AssertionError(f"不应调用工具：{name}")

    repository = SqliteTripRepository(str(tmp_path / "malformed-transfer.sqlite3"))
    provider = RailProvider(MalformedTransferClient(), ProviderExecutor("rail", repository))

    options, cache_hit = await provider.transfers("北京", "杭州", date(2026, 9, 1), "outbound")

    assert options == []
    assert cache_hit is False


@pytest.mark.asyncio
async def test_selected_transfer_queries_segment_prices(tmp_path: Path) -> None:
    class TransferPriceClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            train_code = arguments["train_code"]
            price = "100" if train_code == "G1" else "80"
            return {"data": [{"train_code": train_code, "prices": {"二等座": price}}]}

    client = TransferPriceClient()
    repository = SqliteTripRepository(str(tmp_path / "transfer.sqlite3"))
    provider = RailProvider(client, ProviderExecutor("rail", repository))
    option = RailOption(
        option_id="transfer-1",
        direction="outbound",
        travel_date=date(2026, 9, 1),
        train_code="G1 + D2",
        train_type="中转",
        from_station="北京南",
        to_station="杭州东",
        departure_time="08:00",
        arrival_time="14:00",
        duration_minutes=360,
        is_transfer=True,
        segments=[
            RailSegment(
                train_code="G1",
                from_station="北京南",
                to_station="南京南",
                departure_time="08:00",
                arrival_time="11:00",
                duration_minutes=180,
                seats=[RailSeat(name="二等座", availability="有")],
            ),
            RailSegment(
                train_code="D2",
                from_station="南京南",
                to_station="杭州东",
                departure_time="12:00",
                arrival_time="14:00",
                duration_minutes=120,
                seats=[RailSeat(name="二等座", availability="有")],
            ),
        ],
    )

    quoted = await provider.quote_transfer(option)

    assert quoted.price_from == 180
    assert quoted.seats[0].price == 180
    assert [call[1]["train_code"] for call in client.calls] == ["G1", "D2"]


@pytest.mark.asyncio
async def test_dida_failure_falls_back_to_local_hotels(tmp_path: Path) -> None:
    repository = SqliteTripRepository(str(tmp_path / "hotels.sqlite3"))
    provider = HotelProvider(FailedMcpClient(), ProviderExecutor("hotels", repository))

    options, cache_hit, warning = await provider.search(
        planning_request(), FakeMapProvider().candidates
    )

    assert cache_hit is False
    assert options and options[0].source == "osm"
    assert warning == "酒店实时查询失败，当前展示本地候选。"


@pytest.mark.asyncio
async def test_rollinggo_oauth_search_and_detail_are_mapped_to_hotel_models(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({"access_token": "secret-token"}), encoding="utf-8")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/hotelsearch"):
            return httpx.Response(
                200,
                json={
                    "hotelInformationList": [
                        {
                            "hotelId": 1001,
                            "name": "西湖测试酒店",
                            "address": "湖滨路 1 号",
                            "latitude": 30.25,
                            "longitude": 120.16,
                            "distanceInMeters": 650,
                            "starRating": 4.5,
                            "price": {"hasPrice": True, "lowestPrice": 428},
                            "imageUrl": "https://images.example.com/hotel.jpg",
                            "bookingUrl": "https://booking.example.com/1001",
                            "hotelAmenities": ["Wi-Fi", "停车场"],
                            "tags": ["含早"],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "hotelId": 1001,
                "name": "西湖测试酒店",
                "bookingUrl": "https://booking.example.com/1001",
                "roomRatePlans": [
                    {
                        "ratePlanId": "rate-1",
                        "roomNameCn": "湖景大床房",
                        "ratePlanName": "含双早",
                        "totalPrice": 528,
                        "inventoryCount": 2,
                        "cancellationPolicies": [{"description": "入住前一天可免费取消"}],
                    }
                ],
            },
        )

    repository = SqliteTripRepository(str(tmp_path / "rollinggo.sqlite3"))
    client = RollingGoHotelClient(
        "https://mcp.rollinggo.cn/mcp",
        str(token_path),
        1,
        httpx.MockTransport(handler),
    )
    provider = HotelProvider(client, ProviderExecutor("hotels", repository), "rollinggo")
    request = planning_request()

    options, cache_hit, warning = await provider.search(request, FakeMapProvider().candidates)
    detail, detail_cache_hit = await provider.detail(options[0], request)
    await client.aclose()

    assert cache_hit is False and detail_cache_hit is False and warning is None
    assert options[0].source == "rollinggo"
    assert options[0].price_per_night == 428
    assert options[0].total_price == 428
    assert options[0].distance_km == 0.65
    assert options[0].facilities == ["Wi-Fi", "停车场", "含早"]
    assert detail.source == "rollinggo"
    assert detail.rooms[0].room_id == "rate-1"
    assert detail.rooms[0].name == "湖景大床房"
    assert detail.rooms[0].cancellation == "入住前一天可免费取消"
    assert all(item.headers["Authorization"] == "Bearer secret-token" for item in requests)


@pytest.mark.asyncio
async def test_rollinggo_without_login_falls_back_to_local_hotels(tmp_path: Path) -> None:
    repository = SqliteTripRepository(str(tmp_path / "rollinggo-no-login.sqlite3"))
    client = RollingGoHotelClient(
        "https://mcp.rollinggo.cn/mcp",
        str(tmp_path / "missing-token.json"),
        1,
        httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    provider = HotelProvider(client, ProviderExecutor("hotels", repository), "rollinggo")

    options, cache_hit, warning = await provider.search(
        planning_request(), FakeMapProvider().candidates
    )
    await client.aclose()

    assert cache_hit is False
    assert options and options[0].source == "osm"
    assert warning == "RollingGo 酒店服务未登录，当前展示本地候选。"


@pytest.mark.parametrize("mode", ["invalid", "timeout"])
@pytest.mark.asyncio
async def test_copy_enhancement_failure_keeps_deterministic_itinerary(
    tmp_path: Path, mode: str
) -> None:
    runtime, repository = make_runtime(tmp_path)
    runtime.workflow.copy_enhancer = FailingCopyEnhancer(mode)
    session = runtime.start(planning_request())
    discovered = await wait_for(runtime, session.session_id, "awaiting_selection")
    await runtime.update_selection(
        session.session_id,
        PlanningSelection(
            self_arranged_outbound=True,
            self_arranged_return=True,
            self_arranged_hotel=True,
        ),
    )

    await runtime.generate(discovered.session_id)
    completed = await wait_for(runtime, session.session_id, "completed")

    itinerary = repository.get(session.session_id)
    copy_step = next(item for item in completed.steps if item.name == "copy")
    assert itinerary and itinerary.summary.startswith("已根据交通时刻")
    assert copy_step.status == "degraded"


@pytest.mark.asyncio
async def test_recovery_does_not_save_an_existing_complete_trip_again(tmp_path: Path) -> None:
    repository = CountingRepository(str(tmp_path / "recovery.sqlite3"))
    map_provider = AsyncMapProvider()
    service = TravelService(map_provider, FakePlanner(), repository)
    rail = FakeRailProvider()
    hotels = FakeHotelProvider()
    workflow = WorkbenchWorkflow(service, rail, hotels, DeterministicPlanner())
    runtime = PlanningRuntime(repository, service, workflow, rail, hotels)
    session = runtime.start(planning_request())
    discovered = await wait_for(runtime, session.session_id, "awaiting_selection")
    await runtime.update_selection(
        session.session_id,
        PlanningSelection(
            self_arranged_outbound=True,
            self_arranged_return=True,
            self_arranged_hotel=True,
        ),
    )
    await runtime.generate(discovered.session_id)
    completed = await wait_for(runtime, session.session_id, "completed")
    assert repository.save_calls == 1

    repository.save_session(completed.model_copy(update={"status": "generating"}))
    recovered = PlanningRuntime(repository, service, workflow, rail, hotels)
    await recovered.recover()
    await wait_for(recovered, session.session_id, "completed")

    assert repository.save_calls == 1


def test_mcp_json_and_sse_results_are_supported() -> None:
    json_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "https://example.test/mcp"),
        json={"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": '{"ok":true}'}]}},
    )
    sse_response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        request=httpx.Request("POST", "https://example.test/mcp"),
        text=(
            'event: message\ndata: {"jsonrpc":"2.0","result":{"structuredContent":{"ok":true}}}\n\n'
        ),
    )

    assert _tool_result(_response_payload(json_response)["result"]) == {"ok": True}
    assert _tool_result(_response_payload(sse_response)["result"]) == {"ok": True}


@pytest.mark.asyncio
async def test_mcp_client_initializes_session_before_calling_tool() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"structuredContent": {"ok": True}},
            },
        )

    client = McpHttpClient("https://example.test/mcp", 1)
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.call_tool("query", {"city": "杭州"}) == {"ok": True}
    finally:
        await client.aclose()

    methods = [json.loads(request.content)["method"] for request in requests[:3]]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    assert requests[1].headers["Mcp-Session-Id"] == "session-1"
    assert requests[2].headers["Mcp-Session-Id"] == "session-1"
    assert requests[2].headers["MCP-Protocol-Version"] == "2025-06-18"


@pytest.mark.asyncio
async def test_mcp_client_reinitializes_once_after_session_expiry() -> None:
    initialize_count = 0
    tool_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, tool_count
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-06-18", "capabilities": {}},
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        tool_count += 1
        if tool_count == 1:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"structuredContent": {"ok": True}},
            },
        )

    client = McpHttpClient("https://example.test/mcp", 1)
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.call_tool("query", {}) == {"ok": True}
    finally:
        await client.aclose()

    assert initialize_count == 2
    assert tool_count == 2


def test_sqlite_session_recovery_and_cache_expiry(tmp_path: Path) -> None:
    repository = SqliteTripRepository(str(tmp_path / "state.sqlite3"))
    now = datetime.now(timezone.utc)
    session = PlanningSession(
        session_id=uuid4(),
        request=planning_request(),
        created_at=now,
        updated_at=now,
    )
    repository.create_session(session)
    repository.set_cache("rail", "query", {"ok": True}, 60)

    assert repository.list_recoverable_sessions()[0][1].session_id == session.session_id
    assert repository.get_cache("rail", "query") == {"ok": True}
    repository.set_cache("rail", "expired", {"ok": True}, 0)
    assert repository.get_cache("rail", "expired") is None
