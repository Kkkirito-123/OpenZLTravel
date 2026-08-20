"""TravelGraph 的唯一权威执行状态。"""

from __future__ import annotations

import operator
from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypedDict
from uuid import UUID

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from domain.models import (
    BudgetBreakdown,
    DestinationCandidate,
    ItineraryDraft,
    ReviewResult,
    StrictModel,
    TravelFacts,
    TravelRequirements,
    TravelSelection,
)

TravelPhase = Literal[
    "collecting",
    "discovering",
    "awaiting_selection",
    "planning",
    "reviewing",
    "completed",
    "failed",
    "cancelled",
]


class GraphNotice(StrictModel):
    """可以稳定展示和记录的图警告或错误。

    图状态不能直接保存异常对象：异常通常不可序列化，而且其文本会随依赖版本变化。
    因此节点只写入稳定 ``code``、面向用户的 ``message`` 和责任 ``node``，前端与测试
    都可以在 Checkpoint 重放后得到同一份结果。
    """

    code: str
    message: str
    node: str


def merge_facts(left: TravelFacts | None, right: TravelFacts | Mapping[str, Any]) -> TravelFacts:
    """合并并行 Provider 节点的局部事实，不丢失空结果更新。

    铁路、酒店和天气会并行返回只包含各自字段的 ``TravelFacts``。这里依据
    ``model_fields_set`` 区分“节点没有更新该字段”和“节点明确把该字段更新为空列表”，
    避免某个 Provider 的空结果覆盖其他 Provider 已写入的事实。
    """

    current = left or TravelFacts()
    if isinstance(right, TravelFacts):
        update_fields = right.model_fields_set
        incoming = right.model_dump()
    else:
        update_fields = set(right)
        incoming = dict(right)
    data = current.model_dump()
    for field in update_fields:
        if field in TravelFacts.model_fields:
            data[field] = incoming[field]
    return TravelFacts.model_validate(data)


class TravelState(TypedDict, total=False):
    """整个 Thread 的唯一权威执行状态。

    ``total=False`` 允许每个节点只返回自己负责的增量；字段的写入所有权在下方逐项说明。
    Checkpoint 只持久化这份状态，任何节点都不得另建第二套业务会话或前端影子状态。
    """

    # 对话使用 LangGraph 官方消息 reducer，恢复 Run 时不会重复覆盖既有消息。
    messages: Annotated[list[AnyMessage], add_messages]
    # 仅表示工作流所处阶段；具体路由仍由图中的确定性条件函数决定。
    phase: TravelPhase
    # RequirementAgent 只能修改意图和需求补丁，不能用它直接选择下一节点。
    intent: Literal["destination_discovery", "trip_planning", "unsupported"]
    # 用户明确表达、授权偏好和 clarification resume 合并后的结构化需求。
    requirements: TravelRequirements
    # Catalog 确定性评分后公开给用户的最多五个真实城市，选择后不再保留隐式候选。
    destination_candidates: list[DestinationCandidate]
    # Provider 事实聚合；并行写入必须经过 merge_facts，Agent 永远不能修改这些字段。
    facts: Annotated[TravelFacts, merge_facts]
    # travel_selection interrupt 校验后的用户选择，只保存稳定事实 ID 或自行安排标志。
    selection: TravelSelection
    # PlannerAgent 或确定性降级规划器生成的草稿，最终保存前仍需事实边界校验。
    draft: ItineraryDraft | None
    # ReviewAgent 的只读审查结果；其失败不会绕过最终确定性校验。
    review: ReviewResult | None
    # 只由确定性预算节点计算，未知实时价格保持为 None，不能由 Agent 猜测。
    budget: BudgetBreakdown | None
    # 最终校验后生成的稳定幂等键；同一需求与事实重放时必须保持一致。
    trip_id: UUID | None
    # Review 允许回到 Planner 的次数，当前上限为一次，防止无限自省循环。
    revision_count: int
    # 快速规则是否已足够理解本轮消息，用于决定是否跳过 RequirementAgent。
    fast_understood: bool
    # 可降级问题使用追加 reducer，保留并行 Provider 各自的稳定告警。
    warnings: Annotated[list[GraphNotice], operator.add]
    # 不可继续的问题同样保留来源节点，供失败阶段和客户端统一展示。
    errors: Annotated[list[GraphNotice], operator.add]


class TravelInput(TypedDict):
    """公开图输入，只接收标准消息列表。

    新请求不得直接注入 requirements、facts 或 phase；这些字段只能由图节点从消息和
    Provider 结果构造，防止客户端绕过事实校验。
    """

    messages: list[AnyMessage]


class TravelContext(TypedDict, total=False):
    """不进入业务状态的运行时上下文。

    ``user_id`` 来自 Agent Server 认证结果，仅用于 Store 命名空间和幂等保存；它不会
    接受图输入中的同名值，从而避免用户伪造其他人的所有权。
    """

    user_id: str
