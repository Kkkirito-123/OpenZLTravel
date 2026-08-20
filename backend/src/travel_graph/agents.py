"""三个职责单一、输出严格受控的 LLM Agent。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError

from domain.errors import ModelUnavailableError
from domain.models import (
    ItineraryDraft,
    RequirementPatch,
    ReviewResult,
    StrictModel,
    TravelFacts,
    TravelRequirements,
    TravelSelection,
)
from runtime.contracts import ModelMessage, StructuredModel

ResultT = TypeVar("ResultT", bound=BaseModel)


class RequirementResult(StrictModel):
    """RequirementAgent 只能返回意图和需求补丁。"""

    intent: Literal["destination_discovery", "trip_planning", "unsupported"]
    patch: RequirementPatch = Field(default_factory=RequirementPatch)
    missing_fields: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class RequirementAgent:
    """仅处理规则无法明确识别的需求，超时由图降级。

    它是一个“理解器”，不是路由器：返回值只能补充需求字段，下一步仍由
    ``RequirementNodes.route_after_requirement`` 根据结构化字段决定。
    """

    timeout_seconds = 8.0
    max_tokens = 384

    def __init__(self, model: StructuredModel | None) -> None:
        self.model = model

    async def run(
        self,
        text: str,
        current: TravelRequirements,
        recent_messages: Sequence[str],
    ) -> RequirementResult:
        """从当前消息和最多两轮上下文生成需求补丁。"""

        payload = {
            "current_message": text,
            "current_requirements": current.model_dump(mode="json"),
            "recent_messages": list(recent_messages)[-4:],
            "rules": "只提取用户明确表达的值，不猜测城市、日期或预算。",
        }
        return await _structured_call(
            self.model,
            RequirementResult,
            "你是旅行需求 Agent，只输出符合 schema 的需求结果。",
            payload,
            self.timeout_seconds,
            self.max_tokens,
        )


class PlannerAgent:
    """只从 Provider 事实 ID 中选择并组织分天行程。

    Planner 的结果只是待审核草稿，不能直接写 Store。最终能否保存由确定性
    ``validate_draft``、``validate_routes`` 和预算节点共同决定。
    """

    timeout_seconds = 20.0
    max_tokens = 1800

    def __init__(self, model: StructuredModel | None) -> None:
        self.model = model

    async def run(
        self,
        requirements: TravelRequirements,
        facts: TravelFacts,
        selection: TravelSelection,
        previous: ItineraryDraft | None = None,
        revision_instruction: str | None = None,
    ) -> ItineraryDraft:
        """生成草稿；提示中不提供任何创建事实的工具。"""

        payload = {
            "requirements": requirements.model_dump(mode="json"),
            "selection": selection.model_dump(mode="json"),
            "facts": _planner_facts(facts, selection),
            "previous_draft": previous.model_dump(mode="json") if previous else None,
            "revision_instruction": revision_instruction,
            "rules": "所有 poi_id、meal_ids、hotel_id 必须逐字引用 facts 中已有 ID。",
        }
        return await _structured_call(
            self.model,
            ItineraryDraft,
            "你是受限行程规划 Agent，不得创建任何地点或价格事实。",
            payload,
            self.timeout_seconds,
            self.max_tokens,
        )


class ReviewAgent:
    """只读检查草稿，不修改 Provider 事实或直接保存行程。

    Review 只提供问题和修订指令；图最多把它送回 Planner 一次，之后无论 Review 是否
    可用，都必须继续执行确定性最终校验。
    """

    timeout_seconds = 8.0
    max_tokens = 640

    def __init__(self, model: StructuredModel | None) -> None:
        self.model = model

    async def run(
        self,
        requirements: TravelRequirements,
        facts: TravelFacts,
        draft: ItineraryDraft,
    ) -> ReviewResult:
        """审查节奏、日期和需求匹配；最终事实正确性仍由确定性节点保证。"""

        payload = {
            "requirements": requirements.model_dump(mode="json"),
            "fact_ids": _fact_ids(facts),
            "draft": draft.model_dump(mode="json"),
        }
        return await _structured_call(
            self.model,
            ReviewResult,
            "你是只读行程审查 Agent，只报告问题和修订指令。",
            payload,
            self.timeout_seconds,
            self.max_tokens,
        )


async def _structured_call(
    model: StructuredModel | None,
    result_type: type[ResultT],
    system_prompt: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    max_tokens: int,
) -> ResultT:
    """执行一次有总截止时间的结构化模型调用。

    这里集中处理统一输入、token/耗时限制和 Pydantic 输出校验。节点层负责把异常转换
    为业务降级，因此本函数不做第二次“修复调用”，也不缓存会话级意图结果。
    """

    if model is None:
        raise ModelUnavailableError("model_unavailable", "结构化模型未配置")
    messages: list[ModelMessage] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    raw = await asyncio.wait_for(
        model.ainvoke(messages, response_model=result_type, max_tokens=max_tokens),
        timeout=timeout_seconds,
    )
    data = raw.model_dump() if isinstance(raw, BaseModel) else raw
    try:
        return result_type.model_validate(data)
    except ValidationError as error:
        raise ModelUnavailableError("model_invalid_output", "模型输出不符合结构契约") from error


def _planner_facts(facts: TravelFacts, selection: TravelSelection) -> dict[str, Any]:
    catalog = facts.catalog
    outbound = next(
        (
            item
            for item in facts.outbound_options
            if selection.outbound and item.option_id == selection.outbound.option_id
        ),
        None,
    )
    returning = next(
        (
            item
            for item in facts.return_options
            if selection.return_trip and item.option_id == selection.return_trip.option_id
        ),
        None,
    )
    return {
        "city": facts.city.model_dump(mode="json") if facts.city else None,
        "attractions": [item.model_dump(mode="json") for item in catalog.attractions]
        if catalog
        else [],
        "restaurants": [item.model_dump(mode="json") for item in catalog.restaurants]
        if catalog
        else [],
        "hotels": [item.model_dump(mode="json") for item in facts.hotel_options],
        "selected_outbound": outbound.model_dump(mode="json") if outbound else None,
        "selected_return": returning.model_dump(mode="json") if returning else None,
        "weather": [item.model_dump(mode="json") for item in facts.weather],
    }


def _fact_ids(facts: TravelFacts) -> dict[str, list[str]]:
    catalog = facts.catalog
    return {
        "poi_ids": [item.id for item in catalog.all] if catalog else [],
        "rail_option_ids": [
            *(item.option_id for item in facts.outbound_options),
            *(item.option_id for item in facts.return_options),
        ],
        "hotel_ids": [item.hotel_id for item in facts.hotel_options],
    }
