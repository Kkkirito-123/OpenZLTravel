"""PlannerAgent 与确定性路线、预算节点。

规划阶段故意把“模型推理”和“业务事实”分开：Planner 只生成引用事实 ID 的草稿，
路线和预算由普通 Python 函数完成，审查与最终校验位于相邻的 ``review.py``，保存由
``persistence.py`` 负责。阅读本文件时可以沿着下面的固定流水线理解图：

``plan → build_routes → budget``。

其中 Agent 失败可以降级，但事实边界和保存幂等性不能降级。
"""

from __future__ import annotations

import asyncio
from typing import Any

from domain.errors import ModelUnavailableError, TravelGraphError
from domain.models import (
    City,
    Poi,
    RailChoice,
    RailOption,
    RouteSegment,
    TravelFacts,
    TravelRequirements,
    TravelSelection,
)
from domain.planning import calculate_budget, deterministic_draft, selected_rail
from runtime.contracts import TravelDependencies
from travel_graph.agents import PlannerAgent
from travel_graph.state import GraphNotice, TravelState
from travel_graph.utils import notice


class PlanningNodes:
    """将 PlannerAgent 与路线、预算等确定性节点聚合在规划阶段。

    这个类中的方法会被注册成多个 LangGraph Node；类本身不负责决定顺序，顺序由
    ``workflow.py`` 的 Edge 描述。审查和最终校验由 ``ReviewNodes`` 承担，避免把两种
    不同的安全边界混在同一个类里。
    """

    def __init__(self, dependencies: TravelDependencies) -> None:
        self.dependencies = dependencies
        self.planner = PlannerAgent(dependencies.planner_model)

    async def plan(self, state: TravelState) -> dict[str, Any]:
        """调用 PlannerAgent；失败或 20 秒超时时回退可重现的确定性规划。

        Planner 的输出仍然只是草稿，不能直接保存。后面的路线、预算和最终校验会再次
        从 Provider 事实读取数据，因此“模型说了什么”和“系统允许保存什么”是两层边界。
        """

        requirements = state["requirements"]
        facts = state.get("facts", TravelFacts())
        selection = state.get("selection", TravelSelection())
        previous = state.get("draft")
        review = state.get("review")
        instruction = review.revision_instruction if review else None
        try:
            draft = await self.planner.run(
                requirements,
                facts,
                selection,
                previous,
                instruction,
            )
            return {"draft": draft, "phase": "planning"}
        except TimeoutError:
            return self._fallback_plan(
                requirements,
                facts,
                selection,
                "planner_timeout",
                "行程规划超时，已使用确定性规划。",
            )
        except ModelUnavailableError as error:
            return self._fallback_plan(
                requirements,
                facts,
                selection,
                error.code,
                f"{error.message}，已使用确定性规划。",
            )
        except Exception:
            return self._fallback_plan(
                requirements,
                facts,
                selection,
                "planner_unavailable",
                "行程模型暂时不可用，已使用确定性规划。",
            )

    async def build_routes(self, state: TravelState) -> dict[str, Any]:
        """只对草稿中已存在的真实 POI 查路，未知 ID 留给最终校验拒绝。

        这里先过滤出能在 Catalog 找到的 POI，再调用路线 Provider；过滤只是避免无效的
        外部请求，并不等于放过未知 ID，``final_validate`` 仍会严格拒绝它们。
        """

        facts = state.get("facts", TravelFacts())
        draft = state.get("draft")
        requirements = state["requirements"]
        if draft is None or facts.catalog is None or facts.city is None:
            return {"facts": TravelFacts(routes={})}
        tasks = [
            self._routes_for_day(
                day.day_index,
                [
                    poi
                    for activity in day.activities
                    if (poi := facts.catalog.find(activity.poi_id)) is not None
                ],
                facts.city,
                requirements.transport_mode,
            )
            for day in draft.days
        ]
        results = await asyncio.gather(*tasks)
        routes = {day_index: values for day_index, values, _warnings in results}
        warnings = [warning for _index, _values, items in results for warning in items]
        return {"facts": TravelFacts(routes=routes), "warnings": warnings}

    @staticmethod
    def budget(state: TravelState) -> dict[str, Any]:
        """预算只依赖选中报价和固定估算规则，不调用模型。

        有真实价格就计入真实价格，没有可靠价格就保留估算或 warning；Planner 不能通过
        文本中的数字修改预算。
        """

        draft = state.get("draft")
        if draft is None:
            raise TravelGraphError("draft_missing", "预算计算前缺少行程草稿")
        value = calculate_budget(
            state["requirements"],
            state.get("facts", TravelFacts()),
            state.get("selection", TravelSelection()),
            draft,
        )
        warnings = _missing_price_notices(
            state["requirements"].days_count,
            state.get("facts", TravelFacts()),
            state.get("selection", TravelSelection()),
        )
        return {"budget": value, "phase": "reviewing", "warnings": warnings}

    @staticmethod
    def _fallback_plan(
        requirements: TravelRequirements,
        facts: TravelFacts,
        selection: TravelSelection,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        draft = deterministic_draft(requirements, facts, selection)
        return {
            "draft": draft,
            "phase": "planning",
            "warnings": [notice(code, message, "planner_agent")],
        }

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

def _missing_price_notices(
    days_count: int,
    facts: TravelFacts,
    selection: TravelSelection,
) -> list[GraphNotice]:
    """对选中但缺少真实报价的事实给出明确不计入预算说明。"""

    warnings: list[GraphNotice] = []
    rail_choices = (
        (selection.outbound, facts.outbound_options),
        (selection.return_trip, facts.return_options),
    )
    rail_price_missing = any(
        option is not None and _chosen_rail_price(choice, option) is None
        for choice, options in rail_choices
        if (option := selected_rail(choice, options)) is not None
    )
    if rail_price_missing:
        warnings.append(
            notice(
                "rail_price_unknown",
                "选中车次缺少真实票价，未计入预算总额。",
                "calculate_budget",
            )
        )
    if days_count > 1 and selection.hotel_id and not selection.self_arranged_hotel:
        hotel = next(
            (item for item in facts.hotel_options if item.hotel_id == selection.hotel_id),
            None,
        )
        if hotel and hotel.total_price is None and hotel.price_per_night is None:
            warnings.append(
                notice(
                    "hotel_price_unknown",
                    "选中酒店缺少真实房价，未计入预算总额。",
                    "calculate_budget",
                )
            )
    return warnings


def _chosen_rail_price(choice: RailChoice | None, option: RailOption) -> float | None:
    """从选择和车次事实中读取实际座席价格；未知价格保持为空。"""

    if choice and choice.seat_type:
        seat = next(
            (item for item in option.seats if item.name == choice.seat_type),
            None,
        )
        return seat.price if seat else None
    return option.price_from
