"""把事实服务暴露为 LangChain 可调用工具。"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from langchain.tools import tool

from assistant.fact_service import AssistantFactService


def build_langchain_tools(facts: AssistantFactService) -> list[Any]:
    """创建绑定当前会话事实服务的只读工具集合。"""

    @tool
    async def resolve_place(query: str) -> str:
        """确认城市、景点或地标的真实归属与 POI 信息。"""

        return await _invoke(facts, "resolve_place", facts.resolve_place, query)

    @tool
    async def recommend_destinations(origin: str, region: str) -> str:
        """根据出发地、探索地区和当前偏好推荐真实城市。"""

        return await _invoke(
            facts,
            "recommend_destinations",
            facts.recommend_destinations,
            origin,
            region,
        )

    @tool
    async def search_pois(destination: str) -> str:
        """查询目的地的真实景点、餐厅和基础酒店目录。"""

        return await _invoke(facts, "search_pois", facts.search_pois, destination)

    @tool
    async def search_rail_options(
        origin: str,
        destination: str,
        start_date: str,
        end_date: str,
    ) -> str:
        """查询指定日期的去程和返程铁路候选。"""

        return await _invoke(
            facts,
            "search_rail_options",
            facts.search_rail,
            origin,
            destination,
            date.fromisoformat(start_date),
            date.fromisoformat(end_date),
        )

    @tool
    async def search_hotels() -> str:
        """按当前旅行需求和已查询 POI 查询酒店价格。"""

        return await _invoke(facts, "search_hotels", facts.search_hotels)

    @tool
    async def get_weather() -> str:
        """查询当前目的地和日期范围的天气。"""

        return await _invoke(facts, "get_weather", facts.get_weather)

    return [
        resolve_place,
        recommend_destinations,
        search_pois,
        search_rail_options,
        search_hotels,
        get_weather,
    ]


async def _invoke(
    facts: AssistantFactService,
    name: str,
    operation: Any,
    *args: Any,
) -> str:
    try:
        value = await operation(*args)
    except Exception as error:
        value = facts.record_error(name, error)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
