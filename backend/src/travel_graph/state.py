"""轻量 TravelGraph 的唯一规划状态。

状态只描述规划执行，不描述对话过程。``order`` 和 ``facts`` 是工单验证后的权威输入；
``draft``、``budget`` 和路线修改指令是图内结果；警告、错误和 ``trip_id`` 用于恢复、
确认和最终展示。用户身份放在 ``TravelContext``，初始令牌放在 ``TravelInput``，二者
都不作为普通旅行事实写入业务状态。
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict
from uuid import UUID

from domain.models import (
    BudgetBreakdown,
    ItineraryDraft,
    StrictModel,
    TravelFacts,
    TravelOrder,
)

TravelPhase = Literal["planning", "awaiting_route_confirmation", "completed", "failed"]


class GraphNotice(StrictModel):
    """可序列化、可稳定展示的图警告或错误。"""

    code: str
    message: str
    node: str


class TravelState(TypedDict, total=False):
    """工单进入 Graph 后的最小权威状态。

    状态必须可被 Checkpoint 序列化，因此不能放入 HTTP 客户端、数据库连接、LLM 对象
    或 Provider 实例。
    """

    phase: TravelPhase
    order: TravelOrder
    facts: TravelFacts
    draft: ItineraryDraft
    budget: BudgetBreakdown
    route_revision_instruction: str | None
    trip_id: UUID | None
    warnings: Annotated[list[GraphNotice], operator.add]
    errors: Annotated[list[GraphNotice], operator.add]


class TravelInput(TypedDict):
    """TravelGraph 的唯一外部输入契约。

    ``order_token`` 必须由 Assistant 使用共享 HMAC 密钥签发，并在第一个节点按当前用户
    验证；前端不得把需求、事实或选择直接塞进 Graph 输入。
    """

    order_token: str


class TravelContext(TypedDict, total=False):
    """不进入业务状态的认证上下文。"""

    user_id: str
