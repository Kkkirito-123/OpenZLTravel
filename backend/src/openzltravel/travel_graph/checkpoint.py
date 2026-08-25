"""轻量 TravelGraph 的严格内存 Checkpoint 工厂。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

CHECKPOINT_ALLOWED_MSGPACK_MODULES: tuple[tuple[str, str], ...] = (
    ("openzltravel.domain.models", "TravelRequirements"),
    ("openzltravel.domain.models", "City"),
    ("openzltravel.domain.models", "Poi"),
    ("openzltravel.domain.models", "CandidateCatalog"),
    ("openzltravel.domain.models", "RailSeat"),
    ("openzltravel.domain.models", "RailOption"),
    ("openzltravel.domain.models", "HotelOption"),
    ("openzltravel.domain.models", "WeatherDay"),
    ("openzltravel.domain.models", "RouteSegment"),
    ("openzltravel.domain.models", "RailChoice"),
    ("openzltravel.domain.models", "TravelSelection"),
    ("openzltravel.domain.models", "ActivityDraft"),
    ("openzltravel.domain.models", "DayDraft"),
    ("openzltravel.domain.models", "ItineraryDraft"),
    ("openzltravel.domain.models", "TravelFacts"),
    ("openzltravel.domain.models", "BudgetBreakdown"),
    ("openzltravel.domain.models", "FactStamp"),
    ("openzltravel.domain.models", "TravelOrder"),
    ("openzltravel.travel_graph.state", "GraphNotice"),
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
