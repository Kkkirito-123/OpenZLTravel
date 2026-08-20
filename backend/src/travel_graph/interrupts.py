"""TravelGraph 的三类稳定 interrupt/resume 公开契约。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, TypeVar

from langgraph.types import interrupt
from pydantic import Field, ValidationError

from domain.errors import ResumeValidationError
from domain.models import (
    DestinationCandidate,
    HotelOption,
    RailOption,
    RequirementPatch,
    StrictModel,
    TravelSelection,
)

ResumeT = TypeVar("ResumeT", bound=StrictModel)
ValidatedT = TypeVar("ValidatedT")


class InterruptError(StrictModel):
    """不推进图状态的稳定恢复校验错误。"""

    code: str
    message: str


class ClarificationInterrupt(StrictModel):
    """需求不完整时的追问载荷。

    ``missing_fields`` 是给 UI 生成表单提示的结构化字段；恢复时必须提交
    ``RequirementPatch``，不能把一段未解析的自然语言直接塞回 State。
    """

    kind: Literal["clarification"] = "clarification"
    question: str
    missing_fields: list[str]
    error: InterruptError | None = None


class DestinationSelectionInterrupt(StrictModel):
    """目的地未定时的真实城市候选。

    候选由 Catalog 确定性评分产生，前端只回传其中的 ``candidate_id``；城市名称和坐标
    不从前端恢复载荷读取。
    """

    kind: Literal["destination_selection"] = "destination_selection"
    candidates: list[DestinationCandidate] = Field(max_length=5)
    error: InterruptError | None = None


class TravelSelectionInterrupt(StrictModel):
    """数据发现后的车票和酒店选择载荷。

    这是事实发现和规划之间的边界：Provider 负责提供只读选项，用户只选择稳定 ID 或
    明确声明自行安排，Planner 随后只能使用已经验证过的选择。
    """

    kind: Literal["travel_selection"] = "travel_selection"
    outbound_options: list[RailOption]
    return_options: list[RailOption]
    hotel_options: list[HotelOption]
    requires_hotel: bool
    self_arranged_allowed: bool = True
    error: InterruptError | None = None


class ClarificationResume(StrictModel):
    """对需求追问的结构化回答。"""

    kind: Literal["clarification"]
    values: RequirementPatch


class DestinationSelectionResume(StrictModel):
    """用户选中的城市候选 ID。"""

    kind: Literal["destination_selection"]
    candidate_id: str


class TravelSelectionResume(StrictModel):
    """用户选中的交通与住宿事实 ID。"""

    kind: Literal["travel_selection"]
    selection: TravelSelection


def validate_resume(
    raw: object,
    expected: type[ResumeT],
    kind: str,
) -> ResumeT:
    """在任何状态更新前校验 resume 类型，错误时原子地留在 interrupt。

    把校验放在 interrupt 节点内部，是为了让无效恢复不会先写入半成品 State；调用方
    只需要捕获稳定的 ``ResumeValidationError`` 并重新展示同类中断。
    """

    try:
        return expected.model_validate(raw)
    except ValidationError as error:
        raise ResumeValidationError(
            "invalid_resume_payload",
            f"当前中断只接受 {kind} 类型的恢复载荷",
        ) from error


def interrupt_until_valid(
    payload: StrictModel,
    validator: Callable[[object], ValidatedT],
) -> ValidatedT:
    """无效 resume 不结束 Run，而是以同类 interrupt 返回稳定错误后继续等待。

    LangGraph 在恢复时会从节点开始重放，因此循环中 interrupt 的调用顺序必须始终稳定。
    已消费的错误载荷会在重放时得到同样结果，新的 interrupt 才等待下一次输入。
    """

    current = payload.model_dump(mode="json")
    while True:
        raw = interrupt(current)
        try:
            return validator(raw)
        except ResumeValidationError as error:
            current = payload.model_dump(mode="json")
            current["error"] = {"code": error.code, "message": error.message}
