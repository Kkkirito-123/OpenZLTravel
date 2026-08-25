"""TravelOrder 驱动的轻量规划图契约测试。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from domain.errors import TravelGraphError
from domain.models import (
    CandidateCatalog,
    City,
    Poi,
    RouteSegment,
    TravelFacts,
    TravelOrder,
    TravelRequirements,
    TravelSelection,
)
from runtime.contracts import PlanningDependencies
from runtime.tokens import SignedPayloadCodec
from travel_graph.workflow import build_travel_graph


class CountingRoutes:
    """TravelGraph 唯一允许调用的事实 Provider。"""

    def __init__(self) -> None:
        self.calls = 0

    async def get_routes(
        self,
        city: City,
        day_pois: Sequence[Poi],
        mode: str,
    ) -> tuple[list[RouteSegment], list[str]]:
        del city
        self.calls += 1
        return [
            RouteSegment(
                from_poi_id=left.id,
                to_poi_id=right.id,
                distance_km=2,
                duration_minutes=18,
                mode=mode,
                cost=8,
                source="local_estimate",
            )
            for left, right in zip(day_pois, day_pois[1:], strict=False)
        ], []


def _order() -> TravelOrder:
    attractions = [
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
        Poi(
            id="a3",
            name="景山公园",
            category="attraction",
            latitude=39.92,
            longitude=116.39,
        ),
    ]
    return TravelOrder(
        requirements=TravelRequirements(
            origin="上海",
            destination="北京",
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 2),
            budget=5000,
        ),
        facts=TravelFacts(
            city=City(name="北京", adcode="110000"),
            catalog=CandidateCatalog(
                attractions=attractions,
                required_attraction_ids=[item.id for item in attractions],
            ),
        ),
        selection=TravelSelection(
            attraction_ids=[item.id for item in attractions],
            self_arranged_outbound=True,
            self_arranged_return=True,
            self_arranged_hotel=True,
        ),
    )


def _config(thread_id: str, user_id: str = "user-1") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id, "user_id": user_id}}


def _interrupt(result: dict[str, Any]) -> dict[str, Any]:
    values = result["__interrupt__"]
    assert len(values) == 1
    payload = values[0].value
    assert payload["kind"] == "route_preview"
    return payload


@pytest.mark.asyncio
async def test_valid_order_can_revise_confirm_and_save_idempotently() -> None:
    codec = SignedPayloadCodec("s" * 32)
    order = _order()
    token = codec.issue("travel_order", "user-1", order, 600)
    routes = CountingRoutes()
    store = InMemoryStore()
    graph = build_travel_graph(
        PlanningDependencies(routes=routes),
        codec,
        checkpointer=MemorySaver(),
        store=store,
    )
    run_config = _config("order-run")

    preview = await graph.ainvoke({"order_token": token}, run_config)
    _interrupt(preview)
    assert preview["phase"] == "awaiting_route_confirmation"
    assert routes.calls == 1

    revised = await graph.ainvoke(
        Command(
            resume={
                "kind": "route_preview",
                "action": "message",
                "text": "把故宫放到第二天",
            }
        ),
        run_config,
    )
    _interrupt(revised)
    assert [item.poi_id for item in revised["draft"].days[1].activities] == ["a1"]

    completed = await graph.ainvoke(
        Command(resume={"kind": "route_preview", "action": "confirm"}),
        run_config,
    )
    assert completed["phase"] == "completed"
    assert completed["trip_id"] is not None
    saved = await store.asearch(("user-1", "trips"))
    assert len(saved) == 1
    assert saved[0].value["requirements"]["destination"] == "北京"

    replay = build_travel_graph(
        PlanningDependencies(routes=CountingRoutes()),
        codec,
        checkpointer=MemorySaver(),
        store=store,
    )
    replay_config = _config("order-replay")
    _interrupt(await replay.ainvoke({"order_token": token}, replay_config))
    replayed = await replay.ainvoke(
        Command(resume={"kind": "route_preview", "action": "confirm"}),
        replay_config,
    )
    assert replayed["trip_id"] == completed["trip_id"]
    assert len(await store.asearch(("user-1", "trips"))) == 1


@pytest.mark.asyncio
async def test_graph_rejects_legacy_input_and_cross_user_order() -> None:
    codec = SignedPayloadCodec("s" * 32)
    graph = build_travel_graph(
        PlanningDependencies(routes=CountingRoutes()),
        codec,
        checkpointer=MemorySaver(),
        store=InMemoryStore(),
    )

    with pytest.raises(TravelGraphError) as legacy:
        await graph.ainvoke(
            {"messages": [{"role": "user", "content": "从上海去北京"}]},
            _config("legacy"),
        )
    assert legacy.value.code == "order_token_missing"

    token = codec.issue("travel_order", "user-1", _order(), 600)
    with pytest.raises(TravelGraphError) as cross_user:
        await graph.ainvoke({"order_token": token}, _config("cross-user", "user-2"))
    assert cross_user.value.code == "token_owner_mismatch"


@pytest.mark.asyncio
async def test_unsupported_revision_returns_same_preview_with_explicit_error() -> None:
    codec = SignedPayloadCodec("s" * 32)
    graph = build_travel_graph(
        PlanningDependencies(routes=CountingRoutes()),
        codec,
        checkpointer=MemorySaver(),
        store=InMemoryStore(),
    )
    run_config = _config("unsupported")
    token = codec.issue("travel_order", "user-1", _order(), 600)
    initial = await graph.ainvoke({"order_token": token}, run_config)
    original = initial["draft"]

    rejected = await graph.ainvoke(
        Command(
            resume={
                "kind": "route_preview",
                "action": "message",
                "text": "整体再浪漫一点",
            }
        ),
        run_config,
    )
    payload = _interrupt(rejected)

    assert payload["error"]["code"] == "unsupported_revision"
    assert rejected["draft"] == original
