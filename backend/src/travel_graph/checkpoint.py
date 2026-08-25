"""轻量 TravelGraph 的严格内存 Checkpoint 工厂。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

CHECKPOINT_ALLOWED_MSGPACK_MODULES: tuple[tuple[str, str], ...] = (
    ("domain.models", "TravelRequirements"),
    ("domain.models", "City"),
    ("domain.models", "Poi"),
    ("domain.models", "CandidateCatalog"),
    ("domain.models", "RailSeat"),
    ("domain.models", "RailOption"),
    ("domain.models", "HotelOption"),
    ("domain.models", "WeatherDay"),
    ("domain.models", "RouteSegment"),
    ("domain.models", "RailChoice"),
    ("domain.models", "TravelSelection"),
    ("domain.models", "ActivityDraft"),
    ("domain.models", "DayDraft"),
    ("domain.models", "ItineraryDraft"),
    ("domain.models", "TravelFacts"),
    ("domain.models", "BudgetBreakdown"),
    ("domain.models", "FactStamp"),
    ("domain.models", "TravelOrder"),
    ("travel_graph.state", "GraphNotice"),
)


def create_checkpoint_serializer() -> JsonPlusSerializer:
    """创建无 Pickle 回退的稳定类型序列化器。"""

    return JsonPlusSerializer(
        allowed_msgpack_modules=CHECKPOINT_ALLOWED_MSGPACK_MODULES,
        pickle_fallback=False,
    )


def create_memory_checkpointer() -> InMemorySaver:
    """创建本地运行和离线测试使用的内存 Checkpointer。"""

    return InMemorySaver(serde=create_checkpoint_serializer())


@asynccontextmanager
async def create_checkpointer() -> AsyncIterator[InMemorySaver]:
    """向 Agent Server 提供可正确退出的异步工厂。"""

    saver = create_memory_checkpointer()
    async with saver:
        yield saver
