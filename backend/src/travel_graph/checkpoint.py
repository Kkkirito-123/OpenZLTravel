"""本地 Agent Server 使用的严格异步 Checkpoint 工厂。

本模块只负责 Checkpoint 生命周期与序列化安全边界。生产环境后续可以替换存储后端，
但必须继续复用同一允许列表并保持 ``pickle_fallback=False``，避免持久化数据触发任意
Python 对象反序列化。当前内存实现适用于单进程 ``langgraph dev``，不宣称提供生产持久性。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

# JsonPlusSerializer 自带 datetime、UUID、LangChain 消息和 LangGraph Command/Interrupt
# 等安全类型。这里仅显式增加 TravelState 真实会保存的项目类型；interrupt 会先
# ``model_dump``，TripRecord/PlaceSnapshot 只进入 Store，因此都不扩大 Checkpoint 白名单。
# Agent 客户端、Provider、Runtime、Store 与密钥对象也绝不应进入允许列表。
CHECKPOINT_ALLOWED_MSGPACK_MODULES: tuple[tuple[str, str], ...] = (
    ("domain.models", "TravelRequirements"),
    ("domain.models", "City"),
    ("domain.models", "Poi"),
    ("domain.models", "CandidateCatalog"),
    ("domain.models", "DestinationCandidate"),
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
    ("domain.models", "ReviewIssue"),
    ("domain.models", "ReviewResult"),
    ("domain.models", "TravelFacts"),
    ("domain.models", "BudgetBreakdown"),
    ("travel_graph.state", "GraphNotice"),
)

# 学习提示：图状态中存的是 Pydantic 对象，Checkpoint 必须能把它们编码后再恢复成原类型。
# 允许列表故意写在这里，而不是散落到 langgraph.json，避免“配置看似生效、运行时实际
# 未生效”的版本差异；测试会检查所有真实状态类型都在白名单中。


def create_checkpoint_serializer() -> JsonPlusSerializer:
    """创建只允许稳定 TravelGraph 类型且永不回退 Pickle 的序列化器。"""

    return JsonPlusSerializer(
        allowed_msgpack_modules=CHECKPOINT_ALLOWED_MSGPACK_MODULES,
        pickle_fallback=False,
    )


def create_memory_checkpointer() -> InMemorySaver:
    """创建供本地运行和离线测试使用的严格内存 Checkpointer。"""

    return InMemorySaver(serde=create_checkpoint_serializer())


@asynccontextmanager
async def create_checkpointer() -> AsyncIterator[InMemorySaver]:
    """向 LangGraph Server 提供可正确进入和退出生命周期的异步工厂。

    配置引用的是工厂而不是进程级全局实例，确保开发热重载不会复用已经退出的资源。
    InMemorySaver 同时实现同步和异步协议；服务器运行路径只使用其异步方法。
    """

    saver = create_memory_checkpointer()
    async with saver:
        yield saver
