"""OpenZLTravel 的 LangGraph 编排层。

本模块编译并复用快速规划、数据发现和确定性生成三张图。图只表达节点依赖关系，
供应商解析位于 providers，持久任务生命周期位于 runtime，最终保存仍由运行时统一执行。
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypedDict, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from app.errors import AppError, ProviderError
from app.models import (
    AccommodationPlan,
    CandidateCatalog,
    City,
    IntercityPlan,
    Itinerary,
    ItineraryDraft,
    PlanningRequest,
    Poi,
    RailOption,
    TravelRequest,
    WeatherDay,
)
from app.providers import (
    CopyEnhancer,
    DeterministicPlanner,
    HotelProvider,
    RailProvider,
    TransportResult,
)

StepCallback = Callable[..., Awaitable[None]]


class TravelState(TypedDict, total=False):
    """旧快速入口工作流状态。"""

    request: TravelRequest
    city: City
    candidates: CandidateCatalog
    draft: ItineraryDraft
    weather: list[WeatherDay]
    transport_requirements: dict[int, list[Poi]]
    transport: dict[int, TransportResult]
    transport_warnings: list[str]
    itinerary: Itinerary


class DiscoveryState(TypedDict, total=False):
    """三阶段工作台的数据发现状态。"""

    request: PlanningRequest
    on_step: StepCallback
    city: City
    candidates: CandidateCatalog
    outbound_options: list[RailOption]
    outbound_transfers: list[RailOption]
    return_options: list[RailOption]
    return_transfers: list[RailOption]
    hotel_options: list[Any]
    weather: list[WeatherDay]
    outbound_warnings: list[str]
    return_warnings: list[str]
    hotel_warnings: list[str]
    weather_warnings: list[str]


class GenerationState(TypedDict, total=False):
    """选择完成后的确定性行程生成状态。"""

    session_id: UUID
    request: PlanningRequest
    city: City
    candidates: CandidateCatalog
    weather: list[WeatherDay]
    intercity: IntercityPlan
    accommodation: AccommodationPlan
    warnings: list[str]
    on_step: StepCallback
    draft: ItineraryDraft
    transport_requirements: dict[int, list[Poi]]
    transport: dict[int, TransportResult]
    transport_warnings: list[str]
    itinerary: Itinerary


class TravelWorkflow:
    """旧快速入口使用的兼容图；每个服务实例只编译一次。"""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.graph = self._build_graph()

    async def run(self, request: TravelRequest) -> Itinerary:
        """执行完整图并返回尚未持久化的最终行程。"""

        result = await self.graph.ainvoke({"request": request})
        return cast(Itinerary, result["itinerary"])

    def _build_graph(self) -> Any:
        graph = StateGraph(TravelState)
        graph.add_node("prepare_local_data", self.prepare_local_data)
        graph.add_node("plan_draft", self.plan_draft)
        graph.add_node("fetch_weather", self.fetch_weather)
        graph.add_node("build_transport_requirements", self.build_transport_requirements)
        graph.add_node("fetch_transport", self.fetch_transport)
        graph.add_node("assemble_itinerary", self.assemble_itinerary)
        graph.add_node("validate_itinerary", self.validate_itinerary)
        graph.add_edge(START, "prepare_local_data")
        graph.add_edge("prepare_local_data", "plan_draft")
        graph.add_edge("prepare_local_data", "fetch_weather")
        graph.add_edge(["plan_draft", "fetch_weather"], "build_transport_requirements")
        graph.add_edge("build_transport_requirements", "fetch_transport")
        graph.add_edge("fetch_transport", "assemble_itinerary")
        graph.add_edge("assemble_itinerary", "validate_itinerary")
        graph.add_edge("validate_itinerary", END)
        return graph.compile()

    async def prepare_local_data(self, state: TravelState) -> Mapping[str, Any]:
        """读取城市和候选 POI，命中本地目录时不访问高德发现接口。"""

        request = state["request"]
        provider = self.service.map_provider
        city = await _call_provider(
            provider, "resolve_city_async", "resolve_city", request.destination
        )
        candidates = await _call_provider(
            provider, "search_candidates_async", "search_candidates", city
        )
        if not candidates.attractions:
            raise AppError("no_attractions", f"暂时找不到“{city.name}”的可用景点数据", 422)
        return {"city": city, "candidates": candidates}

    async def plan_draft(self, state: TravelState) -> Mapping[str, Any]:
        """旧入口保留一次受控模型规划和一次修复机会。"""

        draft = await asyncio.to_thread(
            self.service._plan_with_one_repair, state["request"], state["candidates"]
        )
        return {"draft": draft}

    async def fetch_weather(self, state: TravelState) -> Mapping[str, Any]:
        """并行获取天气；Open-Meteo 成功时跳过高德兜底。"""

        request = state["request"]
        weather = await _call_provider(
            self.service.map_provider,
            "get_weather_async",
            "get_weather",
            state["city"],
            request.start_date,
            request.end_date,
        )
        return {"weather": weather}

    async def build_transport_requirements(self, state: TravelState) -> Mapping[str, Any]:
        """把草稿转换成按天的真实 POI 序列。"""

        return {
            "transport_requirements": _transport_requirements(
                state["draft"], state["candidates"]
            )
        }

    async def fetch_transport(self, state: TravelState) -> Mapping[str, Any]:
        """并行准备各日交通，高德调用仍受统一调度器限制。"""

        transport = await _fetch_transport(
            self.service.map_provider,
            state["city"],
            state["transport_requirements"],
            state["request"].transport_mode,
        )
        warnings = [warning for value in transport.values() for warning in value.warnings]
        return {"transport": transport, "transport_warnings": _unique(warnings)}

    async def assemble_itinerary(self, state: TravelState) -> Mapping[str, Any]:
        """将模型草稿与确定性事实合并。"""

        itinerary = self.service.build_itinerary(
            state["request"],
            state["city"],
            state["candidates"],
            state["draft"],
            state["weather"],
            state["transport"],
            state.get("transport_warnings", []),
        )
        return {"itinerary": itinerary}

    async def validate_itinerary(self, state: TravelState) -> Mapping[str, Any]:
        """在保存前重新验证最终结构。"""

        return {"itinerary": Itinerary.model_validate(state["itinerary"].model_dump())}


class WorkbenchWorkflow:
    """编译一次并复用的数据发现图与确定性生成图。"""

    def __init__(
        self,
        service: Any,
        rail: RailProvider,
        hotels: HotelProvider,
        planner: DeterministicPlanner,
        copy_enhancer: CopyEnhancer | None = None,
    ) -> None:
        self.service = service
        self.rail = rail
        self.hotels = hotels
        self.planner = planner
        self.copy_enhancer = copy_enhancer
        self.discovery_graph = self._build_discovery_graph()
        self.generation_graph = self._build_generation_graph()

    async def discover(
        self, request: PlanningRequest, on_step: StepCallback
    ) -> DiscoveryState:
        """并行发现车票、酒店、天气和本地 POI。"""

        result = await self.discovery_graph.ainvoke({"request": request, "on_step": on_step})
        return cast(DiscoveryState, result)

    async def generate(self, state: GenerationState) -> Itinerary:
        """根据用户选择生成完整行程。"""

        result = await self.generation_graph.ainvoke(state)
        return cast(Itinerary, result["itinerary"])

    def _build_discovery_graph(self) -> Any:
        graph = StateGraph(DiscoveryState)
        graph.add_node("prepare_local_data", self._prepare_local_data)
        graph.add_node("fetch_outbound", self._fetch_outbound)
        graph.add_node("fetch_return", self._fetch_return)
        graph.add_node("fetch_hotels", self._fetch_hotels)
        graph.add_node("fetch_weather", self._fetch_workbench_weather)
        graph.add_edge(START, "prepare_local_data")
        for node in ("fetch_outbound", "fetch_return", "fetch_hotels", "fetch_weather"):
            graph.add_edge("prepare_local_data", node)
            graph.add_edge(node, END)
        return graph.compile()

    def _build_generation_graph(self) -> Any:
        graph = StateGraph(GenerationState)
        graph.add_node("plan_draft", self._deterministic_draft)
        graph.add_node("enhance_copy", self._enhance_copy)
        graph.add_node("build_transport_requirements", self._generation_requirements)
        graph.add_node("fetch_transport", self._generation_transport)
        graph.add_node("assemble_itinerary", self._generation_assemble)
        graph.add_node("validate_itinerary", self._generation_validate)
        graph.add_edge(START, "plan_draft")
        graph.add_edge("plan_draft", "enhance_copy")
        graph.add_edge("enhance_copy", "build_transport_requirements")
        graph.add_edge("build_transport_requirements", "fetch_transport")
        graph.add_edge("fetch_transport", "assemble_itinerary")
        graph.add_edge("assemble_itinerary", "validate_itinerary")
        graph.add_edge("validate_itinerary", END)
        return graph.compile()

    async def _prepare_local_data(self, state: DiscoveryState) -> Mapping[str, Any]:
        async def operation() -> tuple[City, CandidateCatalog]:
            provider = self.service.map_provider
            city = await _call_provider(
                provider, "resolve_city_async", "resolve_city", state["request"].destination
            )
            candidates = await _call_provider(
                provider, "search_candidates_async", "search_candidates", city
            )
            if not candidates.attractions:
                raise AppError("no_attractions", "本地目录没有可用景点", 422)
            return city, candidates

        city, candidates = await _step(state, "poi", operation)
        return {"city": city, "candidates": candidates}

    async def _fetch_outbound(self, state: DiscoveryState) -> Mapping[str, Any]:
        request = state["request"]
        return await self._rail_direction(
            state, request.origin, request.destination, request.start_date, "outbound"
        )

    async def _fetch_return(self, state: DiscoveryState) -> Mapping[str, Any]:
        request = state["request"]
        return await self._rail_direction(
            state, request.destination, request.origin, request.end_date, "return"
        )

    async def _rail_direction(
        self,
        state: DiscoveryState,
        origin: str,
        destination: str,
        travel_date: Any,
        direction: str,
    ) -> Mapping[str, Any]:
        step_name = f"rail_{direction}"
        try:
            options, cache_hit = await _step(
                state,
                step_name,
                lambda: self.rail.search(origin, destination, travel_date, direction),
            )
            transfers: list[RailOption] = []
            if not options:
                transfers, transfer_hit = await self.rail.transfers(
                    origin, destination, travel_date, direction
                )
                cache_hit = cache_hit or transfer_hit
            await state["on_step"](step_name, "completed", cache_hit=cache_hit)
            prefix = "outbound" if direction == "outbound" else "return"
            return {f"{prefix}_options": options, f"{prefix}_transfers": transfers}
        except ProviderError as error:
            await state["on_step"](
                step_name,
                "degraded",
                message="12306 暂时不可用，可选择自行安排。",
                error_code=error.code,
            )
            prefix = "outbound" if direction == "outbound" else "return"
            return {
                f"{prefix}_options": [],
                f"{prefix}_transfers": [],
                f"{prefix}_warnings": [
                    "12306 车次查询暂时不可用，请自行确认往返交通。"
                ],
            }

    async def _fetch_hotels(self, state: DiscoveryState) -> Mapping[str, Any]:
        if state["request"].days_count == 1:
            await state["on_step"]("hotels", "completed", message="一日行程无需住宿")
            return {"hotel_options": [], "hotel_warnings": []}
        try:
            options, cache_hit, warning = await _step(
                state,
                "hotels",
                lambda: self.hotels.search(state["request"], state["candidates"]),
            )
            status = "degraded" if warning else "completed"
            await state["on_step"]("hotels", status, cache_hit=cache_hit, message=warning)
            return {"hotel_options": options, "hotel_warnings": [warning] if warning else []}
        except ProviderError as error:
            await state["on_step"](
                "hotels", "degraded", message="酒店实时查询不可用。", error_code=error.code
            )
            return {"hotel_options": [], "hotel_warnings": ["酒店数据暂时不可用，可自行安排住宿。"]}

    async def _fetch_workbench_weather(self, state: DiscoveryState) -> Mapping[str, Any]:
        request = state["request"]
        try:
            weather = await _step(
                state,
                "weather",
                lambda: _call_provider(
                    self.service.map_provider,
                    "get_weather_async",
                    "get_weather",
                    state["city"],
                    request.start_date,
                    request.end_date,
                ),
            )
            return {"weather": weather}
        except ProviderError as error:
            await state["on_step"](
                "weather", "degraded", message="天气暂时不可用。", error_code=error.code
            )
            return {
                "weather": _unknown_weather(request),
                "weather_warnings": ["天气服务暂时不可用，当前日期均标记为未知。"],
            }

    async def _deterministic_draft(self, state: GenerationState) -> Mapping[str, Any]:
        draft = await _step(
            state,
            "planning",
            lambda: asyncio.to_thread(
                self.planner.plan,
                state["request"],
                state["candidates"],
                state["intercity"].outbound,
                state["intercity"].return_trip,
                state["accommodation"].hotel.hotel_id
                if state["accommodation"].hotel
                else None,
            ),
        )
        return {"draft": draft}

    async def _generation_requirements(self, state: GenerationState) -> Mapping[str, Any]:
        return {
            "transport_requirements": _transport_requirements(
                state["draft"], state["candidates"]
            )
        }

    async def _enhance_copy(self, state: GenerationState) -> Mapping[str, Any]:
        """可选文案增强最多等待配置的 8 秒，失败保留确定性草稿。"""

        if self.copy_enhancer is None or not self.copy_enhancer.enabled:
            await state["on_step"]("copy", "completed", message="使用确定性中文模板")
            return {"draft": state["draft"]}
        enhancer = self.copy_enhancer
        try:
            draft = await _step(
                state,
                "copy",
                lambda: asyncio.wait_for(
                    asyncio.to_thread(
                        enhancer.enhance,
                        state["request"],
                        state["draft"],
                    ),
                    timeout=enhancer.settings.llm_enhancement_timeout_seconds,
                ),
            )
            return {"draft": draft}
        except (AppError, asyncio.TimeoutError):
            await state["on_step"](
                "copy", "degraded", message="文案增强不可用，已使用确定性模板"
            )
            return {"draft": state["draft"]}

    async def _generation_transport(self, state: GenerationState) -> Mapping[str, Any]:
        transport = await _step(
            state,
            "transport",
            lambda: _fetch_transport(
                self.service.map_provider,
                state["city"],
                state["transport_requirements"],
                state["request"].transport_mode,
            ),
        )
        warnings = [warning for value in transport.values() for warning in value.warnings]
        return {"transport": transport, "transport_warnings": _unique(warnings)}

    async def _generation_assemble(self, state: GenerationState) -> Mapping[str, Any]:
        itinerary = await _step(
            state,
            "finalize",
            lambda: asyncio.to_thread(
                self.service.build_workbench_itinerary,
                state["request"],
                state["city"],
                state["candidates"],
                state["draft"],
                state["weather"],
                state["transport"],
                state["session_id"],
                state["intercity"],
                state["accommodation"],
                [*state["warnings"], *state.get("transport_warnings", [])],
            ),
        )
        return {"itinerary": itinerary}

    async def _generation_validate(self, state: GenerationState) -> Mapping[str, Any]:
        return {"itinerary": Itinerary.model_validate(state["itinerary"].model_dump())}


async def _step(
    state: Any,
    name: str,
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """记录步骤起止时间，业务节点只关心自己的输入输出。"""

    callback = state["on_step"]
    await callback(name, "running")
    started = time.perf_counter()
    try:
        result = await operation()
    except Exception:
        await callback(name, "failed", duration_ms=_elapsed_ms(started))
        raise
    await callback(name, "completed", duration_ms=_elapsed_ms(started))
    return result


def _transport_requirements(
    draft: ItineraryDraft, candidates: CandidateCatalog
) -> dict[int, list[Poi]]:
    return {
        day.day_index: [
            poi
            for activity in day.activities
            if (poi := candidates.find(activity.poi_id)) is not None
        ]
        for day in draft.days
    }


async def _fetch_transport(
    provider: Any,
    city: City,
    requirements: dict[int, list[Poi]],
    mode: str,
) -> dict[int, TransportResult]:
    async def one(index: int, pois: list[Poi]) -> tuple[int, TransportResult]:
        result = await _call_provider(provider, "get_transport_async", None, city, pois, mode)
        return index, result

    results = await asyncio.gather(*(one(index, pois) for index, pois in requirements.items()))
    return dict(results)


async def _call_provider(
    provider: Any, async_name: str, sync_name: str | None, *args: Any
) -> Any:
    operation = getattr(provider, async_name, None)
    if operation is not None:
        return await operation(*args)
    if sync_name is None:
        raise RuntimeError(f"Provider 缺少能力：{async_name}")
    return await asyncio.to_thread(getattr(provider, sync_name), *args)


def _unknown_weather(request: PlanningRequest) -> list[WeatherDay]:
    return [
        WeatherDay(
            date=request.start_date.fromordinal(request.start_date.toordinal() + offset),
            warning="暂无预报",
        )
        for offset in range(request.days_count)
    ]


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
