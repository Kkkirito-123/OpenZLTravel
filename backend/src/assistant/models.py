"""Assistant SSE、会话快照与结构化模型输出契约。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from domain.models import (
    DestinationCandidate,
    FactStamp,
    RequirementPatch,
    StrictModel,
    TravelFacts,
    TravelOrder,
    TravelRequirements,
    TravelSelection,
)


class AssistantMessage(StrictModel):
    """浏览器当前会话中的一条稳定对话消息。"""

    message_id: str = Field(default_factory=lambda: str(uuid4()))
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1200)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AssistantSnapshot(StrictModel):
    """由签名 Token 保护、由浏览器携带的短期 Assistant 状态。"""

    session_id: UUID = Field(default_factory=uuid4)
    messages: list[AssistantMessage] = Field(default_factory=list, max_length=20)
    requirements: TravelRequirements = Field(default_factory=TravelRequirements)
    destination_candidates: list[DestinationCandidate] = Field(default_factory=list, max_length=5)
    facts: TravelFacts = Field(default_factory=TravelFacts)
    selection: TravelSelection = Field(default_factory=TravelSelection)
    fact_metadata: dict[str, FactStamp] = Field(default_factory=dict)
    status: Literal["collecting", "ready", "submitted"] = "collecting"


class AssistantAction(StrictModel):
    """卡片点击产生的受控选择；ID 必须存在于签名会话事实中。"""

    kind: Literal[
        "select_destination",
        "select_attractions",
        "select_outbound",
        "select_return",
        "select_hotel",
        "self_arrange",
    ]
    candidate_id: str | None = None
    attraction_ids: list[str] = Field(default_factory=list, max_length=28)
    option_id: str | None = None
    seat_type: str | None = None
    hotel_id: str | None = None
    target: Literal["outbound", "return", "hotel"] | None = None


class AssistantTurnRequest(StrictModel):
    """每轮只接受自然语言或一次卡片操作。"""

    session_token: str | None = None
    message: str | None = Field(default=None, min_length=1, max_length=1200)
    action: AssistantAction | None = None

    @model_validator(mode="after")
    def exactly_one_input(self) -> "AssistantTurnRequest":
        if (self.message is None) == (self.action is None):
            raise ValueError("message 与 action 必须且只能提供一个")
        return self


class AssistantDecision(StrictModel):
    """LLM 对当前用户输入的结构化理解，不允许直接创建 Provider 事实。"""

    patch: RequirementPatch = Field(default_factory=RequirementPatch)
    attraction_ids: list[str] = Field(default_factory=list, max_length=28)
    outbound_option_id: str | None = None
    outbound_seat_type: str | None = None
    return_option_id: str | None = None
    return_seat_type: str | None = None
    hotel_id: str | None = None
    self_arranged_outbound: bool = False
    self_arranged_return: bool = False
    self_arranged_hotel: bool = False
    submit_requested: bool = False


class AssistantHandoff(StrictModel):
    """前端用于展示摘要并启动 TravelGraph 的交接事件。"""

    order: TravelOrder
    order_token: str
