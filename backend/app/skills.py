"""旅行助手可执行的 Skill 契约。

Skill 只声明何时启用、需要哪些任务事实以及允许产生什么副作用。LLM 不选择工具，
也看不到供应商参数；真正执行仍由应用服务和 PlanningRuntime 的确定性代码负责。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from app.models import AssistantFlow, AssistantSkillId, AssistantSkillView


@dataclass(frozen=True, slots=True)
class TravelSkill:
    """一个可审计、无动态代码加载的旅行 Skill。"""

    id: AssistantSkillId
    flow: AssistantFlow
    title: str
    description: str
    required_slots: tuple[str, ...]
    effect: Literal["collect_requirements", "start_planning"]

    def view(self) -> AssistantSkillView:
        """返回不含内部实现细节的稳定 API 视图。"""

        return AssistantSkillView(
            id=self.id,
            title=self.title,
            description=self.description,
            required_slots=list(self.required_slots),
            effect=self.effect,
        )

    def context_contract(self) -> str:
        """渲染供意图模型辨别流程的最小元数据，不加载 Skill 正文。"""

        return json.dumps(
            {
                "id": self.id,
                "flow": self.flow,
                "required_slots": self.required_slots,
                "effect": self.effect,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


SKILLS: tuple[TravelSkill, ...] = (
    TravelSkill(
        id="destination_discovery",
        flow="destination_discovery",
        title="目的地需求发现",
        description="整理地区、距离、主题、天数和预算，不编造城市推荐。",
        required_slots=("destination_region|origin", "preferences", "days", "budget"),
        effect="collect_requirements",
    ),
    TravelSkill(
        id="trip_planning",
        flow="trip_planning",
        title="具体行程规划",
        description="收集完整旅行约束后启动车票、酒店、天气和路线工作台。",
        required_slots=(
            "origin",
            "destination_city",
            "start_date",
            "end_date|days",
            "budget",
        ),
        effect="start_planning",
    ),
)

_BY_FLOW = {skill.flow: skill for skill in SKILLS}


def get_skill(flow: AssistantFlow | None) -> TravelSkill | None:
    """根据确定性 Flow 返回唯一 Skill；未选择流程时返回空。"""

    return _BY_FLOW.get(flow) if flow is not None else None


def list_skill_views() -> list[AssistantSkillView]:
    """返回当前应用明确支持的全部 Skill。"""

    return [skill.view() for skill in SKILLS]
