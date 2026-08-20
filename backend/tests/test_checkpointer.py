"""严格 MsgPack Checkpoint 的序列化与 interrupt/resume 回归测试。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from domain.models import CandidateCatalog, City, Poi, TravelFacts, TravelRequirements
from travel_graph.checkpoint import (
    CHECKPOINT_ALLOWED_MSGPACK_MODULES,
    create_checkpoint_serializer,
    create_checkpointer,
    create_memory_checkpointer,
)
from travel_graph.state import GraphNotice

EXPECTED_CHECKPOINT_TYPES = {
    ("domain.models", name)
    for name in {
        "TravelRequirements",
        "City",
        "Poi",
        "CandidateCatalog",
        "DestinationCandidate",
        "RailSeat",
        "RailOption",
        "HotelOption",
        "WeatherDay",
        "RouteSegment",
        "RailChoice",
        "TravelSelection",
        "ActivityDraft",
        "DayDraft",
        "ItineraryDraft",
        "ReviewIssue",
        "ReviewResult",
        "TravelFacts",
        "BudgetBreakdown",
    }
} | {("travel_graph.state", "GraphNotice")}


class CheckpointState(TypedDict, total=False):
    """只包含真实领域对象的最小可中断图状态。"""

    requirements: TravelRequirements
    facts: TravelFacts
    notice: GraphNotice
    answer: str


def test_strict_serializer_round_trip_and_no_pickle_fallback() -> None:
    """允许的领域模型保持类型，不可编码对象也不得静默转成 Pickle。"""

    serializer = create_checkpoint_serializer()
    payload = _checkpoint_payload()
    encoded = serializer.dumps_typed(payload)
    restored = serializer.loads_typed(encoded)

    assert encoded[0] == "msgpack"
    assert serializer.pickle_fallback is False
    assert isinstance(restored["requirements"], TravelRequirements)
    assert isinstance(restored["facts"], TravelFacts)
    assert isinstance(restored["facts"].city, City)
    assert isinstance(restored["notice"], GraphNotice)
    with pytest.raises(TypeError, match="not msgpack serializable"):
        serializer.dumps_typed(lambda: None)


@pytest.mark.asyncio
async def test_custom_async_factory_uses_strict_serializer() -> None:
    """服务器加载的异步工厂必须产出同一严格序列化策略。"""

    async with create_checkpointer() as saver:
        assert isinstance(saver.serde, JsonPlusSerializer)
        assert saver.serde.pickle_fallback is False
        encoded = saver.serde.dumps_typed(_checkpoint_payload())
        assert encoded[0] == "msgpack"


@pytest.mark.asyncio
async def test_strict_checkpoint_survives_interrupt_and_resume() -> None:
    """领域模型经过一次真实 Checkpoint 后仍能恢复原类型并继续执行。"""

    builder = StateGraph(CheckpointState)
    builder.add_node("ask", cast(Any, _ask_destination), input_schema=CheckpointState)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    graph = builder.compile(checkpointer=create_memory_checkpointer())
    config: RunnableConfig = {"configurable": {"thread_id": "strict-checkpoint"}}

    paused = cast(
        dict[str, Any],
        await graph.ainvoke(cast(Any, _checkpoint_payload()), config),
    )
    assert paused["__interrupt__"][0].value["kind"] == "clarification"
    resumed = cast(
        dict[str, Any],
        await graph.ainvoke(
            cast(Any, Command(resume={"destination": "北京"})),
            config,
        ),
    )

    assert resumed["answer"] == "北京"
    assert isinstance(resumed["requirements"], TravelRequirements)
    assert isinstance(resumed["facts"].catalog, CandidateCatalog)
    assert resumed["facts"].catalog.attractions[0].name == "故宫"


def test_langgraph_config_uses_custom_strict_checkpointer() -> None:
    """部署配置只选择自定义后端，序列化白名单由工厂单点负责。"""

    config_path = Path(__file__).resolve().parents[2] / "langgraph.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    checkpointer = config["checkpointer"]

    assert checkpointer["backend"] == "custom"
    assert checkpointer["path"] == (
        "./backend/src/travel_graph/checkpoint.py:create_checkpointer"
    )
    assert "serde" not in checkpointer
    assert set(CHECKPOINT_ALLOWED_MSGPACK_MODULES) == EXPECTED_CHECKPOINT_TYPES


def _ask_destination(_state: CheckpointState) -> dict[str, str]:
    """暂停并读取结构化恢复值。"""

    value = interrupt({"kind": "clarification", "missing_fields": ["destination"]})
    return {"answer": str(value["destination"])}


def _checkpoint_payload() -> CheckpointState:
    """构造同时覆盖嵌套 Pydantic 模型和 GraphNotice 的状态。"""

    city = City(name="北京", adcode="110000", latitude=39.9, longitude=116.4)
    catalog = CandidateCatalog(
        attractions=[
            Poi(
                id="poi:beijing:palace",
                name="故宫",
                category="attraction",
                latitude=39.916,
                longitude=116.397,
            )
        ]
    )
    return {
        "requirements": TravelRequirements(
            origin="杭州",
            destination="北京",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 2),
        ),
        "facts": TravelFacts(city=city, catalog=catalog),
        "notice": GraphNotice(code="checkpoint_test", message="严格序列化", node="test"),
    }
