"""最终事实校验与唯一用户确认节点。"""

from __future__ import annotations

import re
from typing import Any, Literal

from openzltravel.domain.errors import ResumeValidationError, TravelGraphError
from openzltravel.domain.validation import validate_draft, validate_routes, validate_selection
from openzltravel.travel_graph.interrupts import (
    RoutePreviewInterrupt,
    RoutePreviewResume,
    interrupt_until_valid,
    validate_route_resume,
)
from openzltravel.travel_graph.state import TravelState


class ConfirmationNodes:
    """保存前执行确定性校验，并只允许受支持的路线修改。

    ``route_preview`` 是 Graph 唯一的 interrupt。确认会进入保存，支持的修改会回到
    ``build_itinerary`` 重新计算后续路线和预算，无法识别的文本必须显式报错，不能静默
    忽略用户请求。
    """

    @staticmethod
    def validate_plan(state: TravelState) -> dict[str, Any]:
        order = state["order"]
        facts = state.get("facts", order.facts)
        draft = state.get("draft")
        if draft is None:
            raise TravelGraphError("draft_missing", "最终校验缺少行程草稿")
        validate_selection(order.requirements, facts, order.selection)
        validate_draft(order.requirements, facts, order.selection, draft)
        validate_routes(facts, draft)
        return {"phase": "awaiting_route_confirmation"}

    @staticmethod
    def preview(state: TravelState) -> dict[str, Any]:
        order = state["order"]
        budget = state.get("budget")
        budget_limit = order.requirements.budget
        is_over_budget = bool(
            budget is not None
            and budget_limit is not None
            and budget.total_known > budget_limit
        )

        def validate(raw: object) -> RoutePreviewResume:
            resume = validate_route_resume(raw)
            if resume.action == "confirm" and is_over_budget and not resume.allow_over_budget:
                raise ResumeValidationError(
                    "budget_exceeded",
                    "最终行程超出总预算，请调整路线或明确允许超支。",
                )
            if resume.action == "message" and not _supported_revision(state, resume.text or ""):
                raise ResumeValidationError(
                    "unsupported_revision",
                    "当前支持“把某景点放到第 N 天”或“第 N 天少安排一个”。",
                )
            return resume

        resume = interrupt_until_valid(
            RoutePreviewInterrupt(
                budget=budget,
                budget_limit=budget_limit,
                is_over_budget=is_over_budget,
            ),
            validate,
        )
        return {
            "route_revision_instruction": resume.text if resume.action == "message" else None,
            "phase": "planning" if resume.action == "message" else "awaiting_route_confirmation",
        }

    @staticmethod
    def route_after_preview(state: TravelState) -> Literal["revise", "save"]:
        return "revise" if state.get("route_revision_instruction") else "save"


def _supported_revision(state: TravelState, text: str) -> bool:
    if re.search(r"第\s*[一二三四五六七1-7]\s*天.*?(少安排|少放|少一个)", text):
        return True
    match = re.search(r"(.{1,30}?)放(?:到|在)?第\s*[一二三四五六七1-7]\s*天", text)
    if match is None:
        return False
    catalog = state.get("facts", state["order"].facts).catalog
    return bool(catalog and any(item.name in match.group(1) for item in catalog.attractions))
