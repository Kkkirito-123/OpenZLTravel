"""交流助手可调用的只读旅行工具及事实写入边界。

工具函数是 Assistant 唯一的事实入口。每个工具必须同时完成三件事：发出可追踪的
``tool.started`` 事件、调用 Provider 并把经过模型校验的事实写入快照、发出
``tool.result`` 事件。工具失败只能返回明确错误，不能用模型生成的猜测替代上游事实。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from typing import Any

from langchain.tools import tool

from assistant.models import AssistantSnapshot
from domain.models import CandidateCatalog, FactStamp, TravelFacts
from runtime.contracts import AssistantDependencies

AssistantEvent = tuple[str, dict[str, Any]]


class AssistantToolbox:
    """绑定一次请求上下文的 Assistant 工具箱。

    ``snapshot`` 是本轮唯一可变业务状态，``events`` 是本轮临时的展示事件。工具箱不
    持久化数据，也不负责签发 Token；这两项职责分别属于浏览器签名快照和
    ``AssistantService``。
    """

    def __init__(
        self,
        dependencies: AssistantDependencies,
        snapshot: AssistantSnapshot,
    ) -> None:
        self.dependencies = dependencies
        self.snapshot = snapshot
        self.events: list[AssistantEvent] = []

    def langchain_tools(self) -> list[Any]:
        """创建绑定当前请求上下文的 LangChain 工具。"""

        @tool
        async def resolve_place(query: str) -> str:
            """确认城市、景点或地标的真实归属与 POI 信息。"""

            return await self._invoke("resolve_place", self.resolve_place, query)

        @tool
        async def recommend_destinations(origin: str, region: str) -> str:
            """根据出发地、探索地区和当前偏好推荐真实城市。"""

            return await self._invoke(
                "recommend_destinations", self.recommend_destinations, origin, region
            )

        @tool
        async def search_pois(destination: str) -> str:
            """查询目的地的真实景点、餐厅和基础酒店目录。"""

            return await self._invoke("search_pois", self.search_pois, destination)

        @tool
        async def search_rail_options(
            origin: str,
            destination: str,
            start_date: str,
            end_date: str,
        ) -> str:
            """查询指定日期的去程和返程铁路候选。"""

            async def operation() -> dict[str, Any]:
                return await self.search_rail(
                    origin,
                    destination,
                    date.fromisoformat(start_date),
                    date.fromisoformat(end_date),
                )

            return await self._invoke(
                "search_rail_options",
                operation,
            )

        @tool
        async def search_hotels() -> str:
            """按当前旅行需求和已查询 POI 查询酒店价格。"""

            return await self._invoke("search_hotels", self.search_hotels)

        @tool
        async def get_weather() -> str:
            """查询当前目的地和日期范围的天气。"""

            return await self._invoke("get_weather", self.get_weather)

        return [
            resolve_place,
            recommend_destinations,
            search_pois,
            search_rail_options,
            search_hotels,
            get_weather,
        ]

    async def resolve_place(self, query: str) -> dict[str, Any]:
        name = "resolve_place"
        self._started(name)
        result = await self.dependencies.catalog.resolve_place(query)
        payload = result.model_dump(mode="json")
        self._finished(name, "place", payload)
        return payload

    async def recommend_destinations(self, origin: str, region: str) -> list[dict[str, Any]]:
        name = "recommend_destinations"
        self._started(name)
        candidates = await self.dependencies.catalog.recommend_destinations(
            origin,
            region,
            self.snapshot.requirements.preferences,
            limit=5,
        )
        self.snapshot.destination_candidates = candidates[:5]
        self._stamp("destinations", "catalog")
        payload = [item.model_dump(mode="json") for item in self.snapshot.destination_candidates]
        self._finished(name, "destinations", payload)
        return payload

    async def search_pois(self, destination: str) -> dict[str, Any]:
        name = "search_pois"
        self._started(name)
        city = await self.dependencies.catalog.resolve_city(destination)
        catalog = await self.dependencies.catalog.search_candidates(city)
        catalog = CandidateCatalog(
            attractions=catalog.attractions[:12],
            restaurants=catalog.restaurants[:8],
            hotels=catalog.hotels[:6],
        )
        self.snapshot.facts = self.snapshot.facts.model_copy(
            update={"city": city, "catalog": catalog}
        )
        self._stamp("pois", "catalog")
        payload = {
            "city": city.model_dump(mode="json"),
            "catalog": catalog.model_dump(mode="json"),
        }
        self._finished(name, "pois", payload)
        return payload

    async def search_rail(
        self,
        origin: str,
        destination: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        name = "search_rail_options"
        self._started(name)
        outbound, returning = await asyncio.gather(
            self.dependencies.rail.search(origin, destination, start_date, "outbound"),
            self.dependencies.rail.search(origin, destination, end_date, "return"),
        )
        self.snapshot.facts = self.snapshot.facts.model_copy(
            update={
                "outbound_options": outbound[0][:5],
                "return_options": returning[0][:5],
            }
        )
        self._stamp("rail", "12306")
        payload = {
            "outbound_options": [item.model_dump(mode="json") for item in outbound[0][:5]],
            "return_options": [item.model_dump(mode="json") for item in returning[0][:5]],
        }
        self._finished(name, "rail", payload)
        return payload

    async def search_hotels(self) -> list[dict[str, Any]]:
        name = "search_hotels"
        self._started(name)
        catalog = self.snapshot.facts.catalog
        if catalog is None:
            raise ValueError("查询酒店前必须先查询目的地 POI")
        options, _cached, warning = await self.dependencies.hotels.search(
            self.snapshot.requirements,
            catalog,
        )
        self.snapshot.facts = self.snapshot.facts.model_copy(
            update={"hotel_options": options[:6]}
        )
        self._stamp("hotels", "rollinggo")
        payload = [item.model_dump(mode="json") for item in options[:6]]
        self._finished(name, "hotels", {"items": payload, "warning": warning})
        return payload

    async def get_weather(self) -> list[dict[str, Any]]:
        name = "get_weather"
        self._started(name)
        requirements = self.snapshot.requirements
        city = self.snapshot.facts.city
        if city is None or requirements.start_date is None or requirements.end_date is None:
            raise ValueError("查询天气前必须先确认目的地和日期")
        weather = await self.dependencies.weather.get_weather(
            city,
            requirements.start_date,
            requirements.end_date,
        )
        self.snapshot.facts = self.snapshot.facts.model_copy(update={"weather": weather})
        self._stamp("weather", "weather")
        payload = [item.model_dump(mode="json") for item in weather]
        self._finished(name, "weather", payload)
        return payload

    def replace_facts(self, facts: TravelFacts) -> None:
        """提交前刷新完成后原子替换全部时间敏感事实。"""

        self.snapshot.facts = facts

    def _stamp(self, key: str, source: str) -> None:
        metadata = dict(self.snapshot.fact_metadata)
        metadata[key] = FactStamp(source=source, queried_at=datetime.now(timezone.utc))
        self.snapshot.fact_metadata = metadata

    def _started(self, name: str) -> None:
        self.events.append(("tool.started", {"name": name}))

    def _finished(self, name: str, artifact: str, data: Any) -> None:
        self.events.append(
            ("tool.result", {"name": name, "artifact": artifact, "data": data})
        )

    async def _invoke(self, name: str, operation: Any, *args: Any) -> str:
        try:
            return self._json(await operation(*args))
        except Exception as error:
            message = str(error) or "工具暂时不可用"
            self._finished(name, "error", {"error": message})
            return self._json({"error": message})

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
