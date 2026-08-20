"""Catalog 准备、并行事实发现与行程选择节点。

本文件展示 LangGraph 中常见的“扇出—汇合”模式：先准备一份共享的 Catalog，
再让铁路、酒店、天气节点并行查询，各节点只返回自己负责的 ``TravelFacts`` 字段，
由 State reducer 合并结果。Provider 失败会写入 warning 和降级事实，不会因为一个
外部服务不可用而丢失整条旅行流程。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Literal

from domain.errors import ResumeValidationError, TravelGraphError
from domain.models import (
    RailOption,
    TravelFacts,
    TravelRequirements,
    WeatherDay,
)
from domain.validation import validate_selection
from runtime.contracts import TravelDependencies
from travel_graph.interrupts import (
    TravelSelectionInterrupt,
    TravelSelectionResume,
    interrupt_until_valid,
    validate_resume,
)
from travel_graph.state import TravelState
from travel_graph.utils import notice


class DiscoveryNodes:
    """并行查询只写各自的 TravelFacts 子域，由 reducer 安全汇合。

    ``prepare_catalog`` 是并行阶段的前置节点；``evidence_guard`` 是汇合后的检查点；
    ``select_travel`` 则是用户选择车票和酒店的 interrupt。把这三个边界分开后，
    可以清楚看到“事实从哪里来、何时允许用户选择、何时进入 Planner”。
    """

    def __init__(self, dependencies: TravelDependencies) -> None:
        self.dependencies = dependencies

    async def prepare_catalog(self, state: TravelState) -> dict[str, Any]:
        """解析城市并加载 POI；目录完全不可用时不放大为其他请求。

        Catalog 是所有后续 Provider 的事实基座：没有真实城市和景点 ID，就不能安全地
        规划路线。因此目录失败会直接进入 ``failed``，而天气或酒店失败则可以降级继续。
        """

        requirements = state["requirements"]
        if not requirements.destination:
            return _catalog_failure("destination_missing", "数据发现前缺少目的地。")
        try:
            city = await self.dependencies.catalog.resolve_city(requirements.destination)
            catalog = await self.dependencies.catalog.search_candidates(city)
        except Exception:
            return _catalog_failure("catalog_unavailable", "地点目录暂时不可用。")
        if not catalog.attractions:
            return _catalog_failure("no_attractions", "目的地暂无可用景点事实。")
        return {
            "phase": "discovering",
            "facts": TravelFacts(city=city, catalog=catalog),
        }

    async def fetch_rail(self, state: TravelState) -> dict[str, Any]:
        """并行查询去程和返程车次，单边失败时仍允许用户自行安排。

        节点只写 ``outbound_options``、``return_options`` 和 warning；它不修改城市、需求
        或酒店字段，所以可以和另外两个发现节点安全并行。
        """

        requirements = state["requirements"]
        if not _complete_requirements(requirements):
            return {"facts": TravelFacts(outbound_options=[], return_options=[])}
        outbound_task = self._rail_leg(requirements, "outbound")
        return_task = self._rail_leg(requirements, "return")
        outbound, returning = await asyncio.gather(outbound_task, return_task)
        warnings = [*outbound[1], *returning[1]]
        return {
            "facts": TravelFacts(
                outbound_options=outbound[0],
                return_options=returning[0],
            ),
            "warnings": warnings,
        }

    async def fetch_hotels(self, state: TravelState) -> dict[str, Any]:
        """查询酒店；一日游跳过酒店，实时失败时允许 Provider 返回目录候选。

        “跳过酒店”是业务分支，不是异常。返回空列表会让选择模型知道这里没有酒店选择，
        同时由 ``requires_hotel`` 告诉前端是否必须展示住宿选择。
        """

        requirements = state["requirements"]
        facts = state.get("facts", TravelFacts())
        if requirements.days_count <= 1:
            return {"facts": TravelFacts(hotel_options=[])}
        if facts.catalog is None:
            return {"facts": TravelFacts(hotel_options=[])}
        try:
            options, _cache_hit, warning = await self.dependencies.hotels.search(
                requirements,
                facts.catalog,
            )
        except Exception:
            return {
                "facts": TravelFacts(hotel_options=[]),
                "warnings": [
                    notice(
                        "hotel_unavailable",
                        "酒店查询失败，可选择自行安排。",
                        "fetch_hotels",
                    )
                ],
            }
        warnings = [notice("hotel_degraded", warning, "fetch_hotels")] if warning else []
        return {"facts": TravelFacts(hotel_options=options), "warnings": warnings}

    async def fetch_weather(self, state: TravelState) -> dict[str, Any]:
        """查询天气；失败时生成明确的未知占位，不让模型猜测。

        ``source="unknown"`` 是事实边界的一部分：Planner 可以安排活动，但不能把未知
        天气改写成“晴天”。最终行程会保留 warning，让前端知道该信息需要用户自行确认。
        """

        requirements = state["requirements"]
        facts = state.get("facts", TravelFacts())
        if facts.city is None or not requirements.start_date or not requirements.end_date:
            return {"facts": TravelFacts(weather=[])}
        try:
            weather = await self.dependencies.weather.get_weather(
                facts.city,
                requirements.start_date,
                requirements.end_date,
            )
            return {"facts": TravelFacts(weather=weather)}
        except Exception:
            unknown = [
                WeatherDay(
                    date=requirements.start_date + timedelta(days=offset),
                    warning="天气服务暂时不可用。",
                    source="unknown",
                )
                for offset in range(requirements.days_count)
            ]
            return {
                "facts": TravelFacts(weather=unknown),
                "warnings": [
                    notice(
                        "weather_unavailable",
                        "天气查询失败，已保留未知状态。",
                        "fetch_weather",
                    )
                ],
            }

    @staticmethod
    def evidence_guard(state: TravelState) -> dict[str, Any]:
        """并行查询汇合后只改变阶段，不修改任何事实。

        这是一个有意保持很小的汇合节点：所有事实由 reducer 合并完成，Guard 只负责把
        图推进到 ``awaiting_selection``，避免在汇合处再次复制或覆盖 Provider 数据。
        """

        return {"phase": "awaiting_selection"}

    @staticmethod
    def select_travel(state: TravelState) -> dict[str, Any]:
        """暂停等待交通住宿选择，并在恢复时验证事实 ID。

        前端只能提交 interrupt 中出现的稳定 ID；价格、名称和坐标都从当前 State 的事实
        重新查找。这样客户端即使篡改显示文案，也不能把不存在的票或酒店写进最终行程。
        """

        requirements = state["requirements"]
        facts = state.get("facts", TravelFacts())
        payload = TravelSelectionInterrupt(
            outbound_options=facts.outbound_options,
            return_options=facts.return_options,
            hotel_options=facts.hotel_options,
            requires_hotel=requirements.days_count > 1,
        )

        def validate(raw: object) -> TravelSelectionResume:
            """在状态更新前同时校验恢复类型、事实 ID 与一日游住宿约束。"""

            resume = validate_resume(raw, TravelSelectionResume, "travel_selection")
            try:
                validate_selection(requirements, facts, resume.selection)
            except TravelGraphError as error:
                raise ResumeValidationError(error.code, error.message) from error
            return resume

        resume = interrupt_until_valid(payload, validate)
        return {"selection": resume.selection, "phase": "planning"}

    @staticmethod
    def route_after_catalog(state: TravelState) -> Literal["discover", "failed"]:
        """目录是其他 Provider 的前置事实，失败时不继续扇出。"""

        return "failed" if state.get("phase") == "failed" else "discover"

    async def _rail_leg(
        self,
        requirements: TravelRequirements,
        direction: Literal["outbound", "return"],
    ) -> tuple[list[RailOption], list[Any]]:
        travel_date = (
            requirements.start_date if direction == "outbound" else requirements.end_date
        )
        if not requirements.origin or not requirements.destination or travel_date is None:
            return [], []
        try:
            options, _cache_hit = await self.dependencies.rail.search(
                requirements.origin,
                requirements.destination,
                travel_date,
                direction,
            )
            return options, []
        except Exception:
            label = "去程" if direction == "outbound" else "返程"
            return [], [
                notice(
                    f"rail_{direction}_unavailable",
                    f"{label}车次查询失败，可选择自行安排。",
                    "fetch_rail",
                )
            ]


def _catalog_failure(code: str, message: str) -> dict[str, Any]:
    return {
        "phase": "failed",
        "errors": [notice(code, message, "prepare_catalog")],
    }


def _complete_requirements(requirements: TravelRequirements) -> bool:
    return bool(
        requirements.origin
        and requirements.destination
        and requirements.start_date
        and requirements.end_date
    )
