"""TravelOrder 的确定性日程、路线与预算节点。"""

from __future__ import annotations

import asyncio
from typing import Any

from openzltravel.domain.errors import TravelGraphError
from openzltravel.domain.models import City, Poi, RouteSegment
from openzltravel.domain.planning import calculate_budget, deterministic_draft
from openzltravel.runtime.contracts import PlanningDependencies
from openzltravel.travel_graph.state import GraphNotice, TravelState
from openzltravel.travel_graph.utils import notice


class PlanningNodes:
    """规划图只执行可重放的领域规则和最终路线查询。

    日程和预算由 ``domain.planning`` 计算，路线由注入的 RouteGateway 查询。节点不让
    LLM 修改事实，也不重新发现目的地；路线查询只发生在每日 POI 顺序确定之后。
    """

    def __init__(self, dependencies: PlanningDependencies) -> None:
        self.dependencies = dependencies

    async def plan(self, state: TravelState) -> dict[str, Any]:
        order = state["order"]
        draft = deterministic_draft(
            order.requirements,
            state.get("facts", order.facts),
            order.selection,
            state.get("route_revision_instruction"),
        )
        return {
            "draft": draft,
            "phase": "planning",
            "route_revision_instruction": None,
        }

    async def build_routes(self, state: TravelState) -> dict[str, Any]:
        order = state["order"]
        facts = state.get("facts", order.facts)
        draft = state.get("draft")
        if draft is None or facts.catalog is None or facts.city is None:
            raise TravelGraphError("planning_state_incomplete", "路线查询前规划状态不完整")
        days = [
            (
                day.day_index,
                [
                    poi
                    for activity in day.activities
                    if (poi := facts.catalog.find(activity.poi_id)) is not None
                ],
            )
            for day in draft.days
        ]
        results = await asyncio.gather(
            *(
                self._routes_for_day(
                    day_index,
                    pois,
                    facts.city,
                    order.requirements.transport_mode,
                )
                for day_index, pois in days
            )
        )
        routes = {day_index: values for day_index, values, _warnings in results}
        warnings = [warning for _index, _values, values in results for warning in values]
        return {
            "facts": facts.model_copy(update={"routes": routes}),
            "warnings": warnings,
        }

    @staticmethod
    def budget(state: TravelState) -> dict[str, Any]:
        order = state["order"]
        draft = state.get("draft")
        if draft is None:
            raise TravelGraphError("draft_missing", "预算计算前缺少行程草稿")
        budget = calculate_budget(
            order.requirements,
            state.get("facts", order.facts),
            order.selection,
            draft,
        )
        return {"budget": budget, "phase": "planning"}

    async def _routes_for_day(
        self,
        day_index: int,
        pois: list[Poi],
        city: City,
        mode: str,
    ) -> tuple[int, list[RouteSegment], list[GraphNotice]]:
        if len(pois) < 2:
            return day_index, [], []
        try:
            routes, warnings = await self.dependencies.routes.get_routes(city, pois, mode)
            return (
                day_index,
                routes,
                [notice("route_degraded", item, "build_routes") for item in warnings],
            )
        except Exception:
            return (
                day_index,
                [],
                [
                    notice(
                        "route_unavailable",
                        f"第 {day_index} 天路线查询失败，已保留景点安排。",
                        "build_routes",
                    )
                ],
            )
