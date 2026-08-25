"""TravelGraph 唯一的最终路线确认协议。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, TypeVar

from langgraph.types import interrupt
from pydantic import Field, ValidationError, model_validator

from openzltravel.domain.errors import ResumeValidationError
from openzltravel.domain.models import BudgetBreakdown, StrictModel

ValidatedT = TypeVar("ValidatedT")


class InterruptError(StrictModel):
    code: str
    message: str


class RoutePreviewInterrupt(StrictModel):
    kind: Literal["route_preview"] = "route_preview"
    question: str = "路线预览是否合适？确认后会保存最终行程。"
    budget: BudgetBreakdown | None = None
    budget_limit: float | None = Field(default=None, ge=0)
    is_over_budget: bool = False
    error: InterruptError | None = None


class RoutePreviewResume(StrictModel):
    kind: Literal["route_preview"]
    action: Literal["confirm", "message"]
    text: str | None = Field(default=None, max_length=1200)
    allow_over_budget: bool = False

    @model_validator(mode="after")
    def validate_text(self) -> "RoutePreviewResume":
        if self.action == "message" and not self.text:
            raise ValueError("路线调整必须提供 text")
        return self


def validate_route_resume(raw: object) -> RoutePreviewResume:
    try:
        return RoutePreviewResume.model_validate(raw)
    except ValidationError as error:
        raise ResumeValidationError(
            "invalid_resume_payload",
            "当前中断只接受 route_preview 类型的恢复载荷",
        ) from error


def interrupt_until_valid(
    payload: StrictModel,
    validator: Callable[[object], ValidatedT],
) -> ValidatedT:
    current = payload.model_dump(mode="json")
    while True:
        raw = interrupt(current)
        try:
            return validator(raw)
        except ResumeValidationError as error:
            current = payload.model_dump(mode="json")
            current["error"] = {"code": error.code, "message": error.message}
