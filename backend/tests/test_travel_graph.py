"""当前 TravelGraph 的离线契约、事实边界与降级测试。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import BaseModel

from domain.errors import FactBoundaryError
from domain.models import (
    ActivityDraft,
    CandidateCatalog,
    City,
    DayDraft,
    DestinationCandidate,
    HotelOption,
    ItineraryDraft,
    Poi,
    RailOption,
    RouteSegment,
    WeatherDay,
)
from runtime.contracts import ModelMessage, TravelDependencies
from travel_graph.checkpoint import create_memory_checkpointer
from travel_graph.workflow import build_travel_graph


class FakeModel:
    """按顺序返回结构化结果或抛出预设异常。"""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def ainvoke(
        self,
        messages: Sequence[ModelMessage],
        *,
        response_model: type[BaseModel],
        max_tokens: int,
    ) -> BaseModel | dict[str, Any]:
        del messages, response_model, max_tokens
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, (BaseModel, dict))
        return response


class FakeCatalog:
    """提供两个真实城市和稳定 POI ID。"""

    async def resolve_city(self, destination: str) -> City:
        return City(name=destination, adcode="110000", latitude=39.9, longitude=116.4)

    async def search_candidates(self, city: City) -> CandidateCatalog:
        del city
        return CandidateCatalog(
            attractions=[
                Poi(
                    id="a1",
                    name="故宫",
                    category="attraction",
                    latitude=39.91,
                    longitude=116.39,
                ),
                Poi(
                    id="a2",
                    name="天坛",
                    category="attraction",
                    latitude=39.88,
                    longitude=116.41,
                ),
            ],
            restaurants=[
                Poi(
                    id="r1",
                    name="本地餐厅",
                    category="restaurant",
                    latitude=39.9,
                    longitude=116.4,
                )
            ],
            hotels=[
                Poi(
                    id="catalog-h1",
                    name="目录酒店",
                    category="hotel",
                    latitude=39.9,
                    longitude=116.4,
                )
            ],
        )

    async def recommend_destinations(
        self,
        origin: str,
        region: str,
        preferences: list[str],
        limit: int = 5,
    ) -> list[DestinationCandidate]:
        del origin, region, preferences, limit
        return [
            DestinationCandidate(
                candidate_id="beijing",
                city=City(name="北京"),
                score=0.91,
                reasons=["景点覆盖充足"],
                attraction_count=20,
                restaurant_count=12,
                hotel_count=8,
            )
        ]


class FakeRail:
    """返回每个方向一条稳定车次。"""

    async def search(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        direction: str,
    ) -> tuple[list[RailOption], bool]:
        return [
            RailOption(
                option_id=f"{direction}-1",
                direction=direction,
                travel_date=travel_date,
                train_code="G1",
                from_station=origin,
                to_station=destination,
                departure_time="08:00",
                arrival_time="12:00",
                duration_minutes=240,
                price_from=500,
                has_ticket=True,
            )
        ], False


class FakeHotels:
    """返回一条实时酒店事实。"""

    async def search(
        self,
        requirements: Any,
        catalog: CandidateCatalog,
    ) -> tuple[list[HotelOption], bool, str | None]:
        del requirements, catalog
        return [
            HotelOption(
                hotel_id="h1",
                name="旅行酒店",
                total_price=600,
                source="rollinggo",
            )
        ], False, None


class NoPriceRail(FakeRail):
    """返回可选但没有真实票价的车次。"""

    async def search(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        direction: str,
    ) -> tuple[list[RailOption], bool]:
        options, cache_hit = await super().search(
            origin,
            destination,
            travel_date,
            direction,
        )
        return [item.model_copy(update={"price_from": None}) for item in options], cache_hit


class NoPriceHotels(FakeHotels):
    """返回可选但没有真实房价的酒店。"""

    async def search(
        self,
        requirements: Any,
        catalog: CandidateCatalog,
    ) -> tuple[list[HotelOption], bool, str | None]:
        del requirements, catalog
        return [HotelOption(hotel_id="h1", name="未知价酒店", source="rollinggo")], False, None


class CountingHotels(FakeHotels):
    """用于确认一日游不发起酒店请求。"""

    def __init__(self) -> None:
        self.calls = 0

    async def search(
        self,
        requirements: Any,
        catalog: CandidateCatalog,
    ) -> tuple[list[HotelOption], bool, str | None]:
        self.calls += 1
        return await super().search(requirements, catalog)


class FailingRail(FakeRail):
    """模拟去返程都不可用。"""

    async def search(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        direction: str,
    ) -> tuple[list[RailOption], bool]:
        del origin, destination, travel_date, direction
        raise RuntimeError("rail down")


class FailingHotels(FakeHotels):
    """模拟实时和本地酒店都不可用。"""

    async def search(
        self,
        requirements: Any,
        catalog: CandidateCatalog,
    ) -> tuple[list[HotelOption], bool, str | None]:
        del requirements, catalog
        raise RuntimeError("hotel down")


class FakeWeather:
    """返回完整天气范围。"""

    async def get_weather(
        self,
        city: City,
        start_date: date,
        end_date: date,
    ) -> list[WeatherDay]:
        del city
        return [
            WeatherDay(date=start_date, day_weather="晴", source="open_meteo"),
            *(
                [WeatherDay(date=end_date, day_weather="多云", source="open_meteo")]
                if end_date != start_date
                else []
            ),
        ]


class FailingWeather(FakeWeather):
    """模拟天气主来源和兜底来源都失败。"""

    async def get_weather(
        self,
        city: City,
        start_date: date,
        end_date: date,
    ) -> list[WeatherDay]:
        del city, start_date, end_date
        raise RuntimeError("weather down")


class FakeRoutes:
    """返回相邻 POI 之间的本地估算路线。"""

    async def get_routes(
        self,
        city: City,
        day_pois: Sequence[Poi],
        mode: str,
    ) -> tuple[list[RouteSegment], list[str]]:
        del city
        return [
            RouteSegment(
                from_poi_id=left.id,
                to_poi_id=right.id,
                distance_km=3,
                duration_minutes=20,
                mode=mode,
                cost=10,
                source="local_estimate",
            )
            for left, right in zip(day_pois, day_pois[1:], strict=False)
        ], []


class FailingRoutes(FakeRoutes):
    """模拟路线服务完全不可用。"""

    async def get_routes(
        self,
        city: City,
        day_pois: Sequence[Poi],
        mode: str,
    ) -> tuple[list[RouteSegment], list[str]]:
        del city, day_pois, mode
        raise RuntimeError("routes down")


def dependencies(
    *,
    requirement_model: FakeModel | None = None,
    planner_model: FakeModel | None = None,
    review_model: FakeModel | None = None,
    rail: Any | None = None,
    hotels: Any | None = None,
    weather: Any | None = None,
    routes: Any | None = None,
) -> TravelDependencies:
    """构造不访问网络的完整图依赖。"""

    return TravelDependencies(
        catalog=FakeCatalog(),
        rail=rail or FakeRail(),
        hotels=hotels or FakeHotels(),
        weather=weather or FakeWeather(),
        routes=routes or FakeRoutes(),
        requirement_model=requirement_model,
        planner_model=planner_model,
        review_model=review_model,
    )


def config(thread_id: str, user_id: str = "user-1") -> dict[str, Any]:
    """构造带线程和离线身份的运行配置。"""

    return {"configurable": {"thread_id": thread_id, "user_id": user_id}}


def interrupt_value(result: dict[str, Any], kind: str) -> dict[str, Any]:
    """取出并校验当前唯一中断。"""

    values = result["__interrupt__"]
    assert len(values) == 1
    value = values[0].value
    assert value["kind"] == kind
    return value


def travel_resume(*, hotel: bool = True) -> Command:
    """构造引用当前 Fake Provider 事实 ID 的选择。"""

    return Command(
        resume={
            "kind": "travel_selection",
            "selection": {
                "outbound": {"option_id": "outbound-1"},
                "return_trip": {"option_id": "return-1"},
                "hotel_id": "h1" if hotel else None,
                "self_arranged_hotel": False,
            },
        }
    )


@pytest.mark.asyncio
async def test_complete_request_falls_back_and_saves_idempotently() -> None:
    """完整需求跳过意图 LLM，Planner/Review 未配置仍能完成并幂等保存。"""

    store = InMemoryStore()
    requirement_model = FakeModel()
    graph = build_travel_graph(
        dependencies(requirement_model=requirement_model),
        # 主干流程使用与 Agent Server 相同的严格序列化器，覆盖事实、选择、草稿与完成态。
        checkpointer=create_memory_checkpointer(),
        store=store,
    )
    run_config = {
        "configurable": {
            "thread_id": "complete",
            "user_id": "must-not-win",
            "langgraph_auth_user_id": "user-1",
        }
    }
    result = await graph.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "从杭州去北京玩2天，2026-09-01到2026-09-02，2人预算5000",
                }
            ]
        },
        run_config,
    )
    interrupt_value(result, "travel_selection")
    assert result["facts"].outbound_options
    assert result["facts"].return_options
    assert result["facts"].hotel_options[0].hotel_id == "h1"
    assert len(result["facts"].weather) == 2
    completed = await graph.ainvoke(travel_resume(), run_config)
    assert completed["phase"] == "completed"
    items = await store.asearch(("user-1", "trips"))
    assert len(items) == 1
    assert items[0].value["trip_id"] == str(completed["trip_id"])
    assert items[0].value["place_index"]["a1"]["name"] == "故宫"
    assert items[0].value["place_index"]["h1"]["name"] == "旅行酒店"
    assert await store.asearch(("another-user", "trips")) == []
    assert requirement_model.calls == 0

    # 模拟 Checkpointer 丢失后的整图重跑：Store 中的稳定键仍不会生成第二条行程。
    replay = build_travel_graph(dependencies(), checkpointer=MemorySaver(), store=store)
    replay_initial = await replay.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "从杭州去北京玩2天，2026-09-01到2026-09-02，2人预算5000",
                }
            ]
        },
        run_config,
    )
    interrupt_value(replay_initial, "travel_selection")
    replayed = await replay.ainvoke(travel_resume(), run_config)
    assert replayed["trip_id"] == completed["trip_id"]
    assert len(await store.asearch(("user-1", "trips"))) == 1


@pytest.mark.asyncio
async def test_clarification_can_resume_more_than_once() -> None:
    """每次只补部分槽位时，图会再次中断而不丢失已收集值。"""

    graph = build_travel_graph(
        dependencies(),
        checkpointer=MemorySaver(),
        store=InMemoryStore(),
    )
    run_config = config("clarify")
    first = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "从杭州出发"}]},
        run_config,
    )
    interrupt_value(first, "clarification")
    second = await graph.ainvoke(
        Command(
            resume={
                "kind": "clarification",
                "values": {"destination": "北京"},
            }
        ),
        run_config,
    )
    interrupt_value(second, "clarification")
    third = await graph.ainvoke(
        Command(
            resume={
                "kind": "clarification",
                "values": {
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-02",
                },
            }
        ),
        run_config,
    )
    interrupt_value(third, "travel_selection")


@pytest.mark.asyncio
async def test_destination_selection_and_wrong_resume_type() -> None:
    """地区型需求使用真实城市候选，错误 resume 类型不推进状态。"""

    graph = build_travel_graph(
        dependencies(),
        checkpointer=MemorySaver(),
        store=InMemoryStore(),
    )
    run_config = config("destination")
    result = await graph.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "从杭州出发，华东玩2天，2026-09-01到2026-09-02",
                }
            ]
        },
        run_config,
    )
    payload = interrupt_value(result, "destination_selection")
    assert payload["candidates"][0]["candidate_id"] == "beijing"
    invalid = await graph.ainvoke(
        Command(resume={"kind": "clarification", "values": {}}),
        run_config,
    )
    retry = interrupt_value(invalid, "destination_selection")
    assert retry["error"]["code"] == "invalid_resume_payload"
    selected = await graph.ainvoke(
        Command(
            resume={
                "kind": "destination_selection",
                "candidate_id": "beijing",
            }
        ),
        run_config,
    )
    interrupt_value(selected, "travel_selection")


@pytest.mark.asyncio
async def test_requirement_timeout_degrades_to_clarification() -> None:
    """需求模型超时不使图失败，而是转入确定性追问。"""

    model = FakeModel(TimeoutError())
    graph = build_travel_graph(
        dependencies(requirement_model=model),
        checkpointer=MemorySaver(),
        store=InMemoryStore(),
    )
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "帮我规划一下"}]},
        config("requirement-timeout"),
    )
    interrupt_value(result, "clarification")
    assert any(item.code == "requirement_timeout" for item in result["warnings"])


@pytest.mark.asyncio
async def test_review_revises_at_most_once() -> None:
    """审查连续不通过时也只返回 PlannerAgent 一次。"""

    draft = ItineraryDraft(
        summary="真实地点行程",
        days=[
            DayDraft(
                day_index=1,
                theme="故宫",
                activities=[ActivityDraft(poi_id="a1", start_time="09:00")],
                meal_ids=["r1"],
                hotel_id="h1",
            ),
            DayDraft(
                day_index=2,
                theme="天坛",
                activities=[ActivityDraft(poi_id="a2", start_time="09:00")],
                meal_ids=["r1"],
            ),
        ],
    )
    planner = FakeModel(draft, draft)
    failed_review = {"passed": False, "issues": [], "revision_instruction": "放松节奏"}
    reviewer = FakeModel(failed_review, failed_review)
    graph = build_travel_graph(
        dependencies(planner_model=planner, review_model=reviewer),
        checkpointer=MemorySaver(),
        store=InMemoryStore(),
    )
    run_config = config("review")
    initial = await graph.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "从杭州去北京玩2天，2026-09-01到2026-09-02",
                }
            ]
        },
        run_config,
    )
    interrupt_value(initial, "travel_selection")
    result = await graph.ainvoke(travel_resume(), run_config)
    assert result["phase"] == "completed"
    assert result["revision_count"] == 1
    assert planner.calls == 2
    assert reviewer.calls == 2


@pytest.mark.asyncio
async def test_unknown_agent_fact_id_is_rejected_before_save() -> None:
    """PlannerAgent 结构合法但编造 ID 时，最终校验必须拒绝且 Store 不留半成品。"""

    invalid = ItineraryDraft(
        summary="包含伪造地点",
        days=[
            DayDraft(
                day_index=1,
                theme="未知",
                activities=[ActivityDraft(poi_id="invented", start_time="09:00")],
                hotel_id="h1",
            ),
            DayDraft(day_index=2, theme="返程"),
        ],
    )
    store = InMemoryStore()
    graph = build_travel_graph(
        dependencies(planner_model=FakeModel(invalid)),
        checkpointer=MemorySaver(),
        store=store,
    )
    run_config = config("invalid-fact")
    initial = await graph.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "从杭州去北京玩2天，2026-09-01到2026-09-02",
                }
            ]
        },
        run_config,
    )
    interrupt_value(initial, "travel_selection")
    with pytest.raises(FactBoundaryError):
        await graph.ainvoke(travel_resume(), run_config)
    assert await store.asearch(("user-1", "trips")) == []


@pytest.mark.asyncio
async def test_agent_cannot_replace_the_selected_hotel_with_another_valid_hotel() -> None:
    """Planner 即使引用目录中存在的酒店，也不能覆盖用户已选择的酒店。"""

    mismatched = ItineraryDraft(
        summary="擅自更换酒店",
        days=[
            DayDraft(day_index=1, theme="抵达", hotel_id="catalog-h1"),
            DayDraft(day_index=2, theme="返程"),
        ],
    )
    store = InMemoryStore()
    graph = build_travel_graph(
        dependencies(planner_model=FakeModel(mismatched)),
        checkpointer=MemorySaver(),
        store=store,
    )
    run_config = config("hotel-selection-mismatch")
    initial = await graph.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "从杭州去北京玩2天，2026-09-01到2026-09-02",
                }
            ]
        },
        run_config,
    )
    interrupt_value(initial, "travel_selection")
    with pytest.raises(FactBoundaryError, match="酒店安排与用户选择不一致"):
        await graph.ainvoke(travel_resume(), run_config)
    assert await store.asearch(("user-1", "trips")) == []


@pytest.mark.asyncio
async def test_one_day_trip_skips_hotel_provider() -> None:
    """一日游不查酒店，恢复载荷也不要求住宿选择。"""

    hotels = CountingHotels()
    graph = build_travel_graph(
        dependencies(hotels=hotels),
        checkpointer=MemorySaver(),
        store=InMemoryStore(),
    )
    run_config = config("one-day")
    initial = await graph.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "从杭州去北京玩1天，2026-09-01",
                }
            ]
        },
        run_config,
    )
    payload = interrupt_value(initial, "travel_selection")
    assert payload["requires_hotel"] is False
    assert hotels.calls == 0
    completed = await graph.ainvoke(travel_resume(hotel=False), run_config)
    assert completed["phase"] == "completed"


@pytest.mark.asyncio
async def test_planner_and_review_timeouts_have_distinct_fallbacks() -> None:
    """Planner 超时回退确定性草稿，Review 超时只跳过语义审查。"""

    graph = build_travel_graph(
        dependencies(
            planner_model=FakeModel(TimeoutError()),
            review_model=FakeModel(TimeoutError()),
        ),
        checkpointer=MemorySaver(),
        store=InMemoryStore(),
    )
    run_config = config("model-timeouts")
    initial = await graph.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "从杭州去北京玩2天，2026-09-01到2026-09-02",
                }
            ]
        },
        run_config,
    )
    interrupt_value(initial, "travel_selection")
    completed = await graph.ainvoke(travel_resume(), run_config)
    codes = {item.code for item in completed["warnings"]}
    assert completed["phase"] == "completed"
    assert {"planner_timeout", "review_timeout"} <= codes


@pytest.mark.asyncio
async def test_unknown_selected_prices_are_not_added_to_budget() -> None:
    """车票和房价未知时保持 None，不猜价且各自生成稳定警告。"""

    graph = build_travel_graph(
        dependencies(rail=NoPriceRail(), hotels=NoPriceHotels()),
        checkpointer=MemorySaver(),
        store=InMemoryStore(),
    )
    run_config = config("unknown-prices")
    initial = await graph.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "从杭州去北京玩2天，2026-09-01到2026-09-02",
                }
            ]
        },
        run_config,
    )
    interrupt_value(initial, "travel_selection")
    completed = await graph.ainvoke(travel_resume(), run_config)
    assert completed["budget"].intercity_transport is None
    assert completed["budget"].hotel is None
    codes = {item.code for item in completed["warnings"]}
    assert {"rail_price_unknown", "hotel_price_unknown"} <= codes


@pytest.mark.asyncio
async def test_provider_failures_degrade_independently() -> None:
    """铁路、酒店、天气和路线失败只添加各自警告，不相互覆盖事实。"""

    draft = ItineraryDraft(
        summary="可降级行程",
        days=[
            DayDraft(
                day_index=1,
                theme="城市漫步",
                activities=[
                    ActivityDraft(poi_id="a1", start_time="09:00"),
                    ActivityDraft(poi_id="a2", start_time="14:00"),
                ],
                meal_ids=["r1"],
            ),
            DayDraft(day_index=2, theme="返程"),
        ],
    )
    graph = build_travel_graph(
        dependencies(
            planner_model=FakeModel(draft),
            rail=FailingRail(),
            hotels=FailingHotels(),
            weather=FailingWeather(),
            routes=FailingRoutes(),
        ),
        checkpointer=MemorySaver(),
        store=InMemoryStore(),
    )
    run_config = config("provider-failures")
    initial = await graph.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "从杭州去北京玩2天，2026-09-01到2026-09-02",
                }
            ]
        },
        run_config,
    )
    payload = interrupt_value(initial, "travel_selection")
    assert payload["outbound_options"] == []
    assert payload["hotel_options"] == []
    completed = await graph.ainvoke(
        Command(
            resume={
                "kind": "travel_selection",
                "selection": {
                    "self_arranged_outbound": True,
                    "self_arranged_return": True,
                    "self_arranged_hotel": True,
                },
            }
        ),
        run_config,
    )
    codes = {item.code for item in completed["warnings"]}
    assert completed["phase"] == "completed"
    assert {
        "rail_outbound_unavailable",
        "rail_return_unavailable",
        "hotel_unavailable",
        "weather_unavailable",
        "route_unavailable",
    } <= codes


@pytest.mark.asyncio
async def test_preferences_require_explicit_memory_command_and_are_namespaced() -> None:
    """只有“记住”指令写入偏好 Store，且不会泄漏给其他用户。"""

    store = InMemoryStore()
    graph = build_travel_graph(dependencies(), checkpointer=MemorySaver(), store=store)
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "记住我的出发地是杭州"}]},
        config("memory", "memory-user"),
    )
    interrupt_value(result, "clarification")
    item = await store.aget(("memory-user", "preferences"), "stable")
    assert item is not None
    assert item.value == {"origin": "杭州"}
    assert await store.aget(("other-user", "preferences"), "stable") is None

    implicit = build_travel_graph(dependencies(), checkpointer=MemorySaver(), store=store)
    implicit_result = await implicit.ainvoke(
        {"messages": [{"role": "user", "content": "我从上海出发"}]},
        config("implicit-memory", "implicit-user"),
    )
    interrupt_value(implicit_result, "clarification")
    assert await store.aget(("implicit-user", "preferences"), "stable") is None

    forgetting = build_travel_graph(dependencies(), checkpointer=MemorySaver(), store=store)
    forgotten_result = await forgetting.ainvoke(
        {"messages": [{"role": "user", "content": "忘记出发地"}]},
        config("forget-memory", "memory-user"),
    )
    interrupt_value(forgotten_result, "clarification")
    assert await store.aget(("memory-user", "preferences"), "stable") is None
