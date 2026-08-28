"""Assistant 的只读事实查询与会话事实写入边界。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any

from assistant.models import AssistantSnapshot
from domain.models import CandidateCatalog, FactStamp
from runtime.contracts import AssistantDependencies

AssistantEvent = tuple[str, dict[str, Any]]


class AssistantFactService:
    """调用 Provider 并把验证后的事实写入当前会话快照。"""

    def __init__(
        self,
        dependencies: AssistantDependencies,
        snapshot: AssistantSnapshot,
    ) -> None:
        self.dependencies = dependencies
        self.snapshot = snapshot
        self.events: list[AssistantEvent] = []

    async def resolve_place(self, query: str) -> dict[str, Any]:
        result = await self.dependencies.catalog.resolve_place(query)
        payload = result.model_dump(mode="json")
        self._result("resolve_place", "place", payload)
        return payload

    async def recommend_destinations(self, origin: str, region: str) -> list[dict[str, Any]]:
        candidates = await self.dependencies.catalog.recommend_destinations(
            origin,
            region,
            self.snapshot.requirements.preferences,
            limit=5,
        )
        self.snapshot.destination_candidates = candidates[:5]
        self._stamp("destinations", "catalog")
        payload = [item.model_dump(mode="json") for item in self.snapshot.destination_candidates]
        self._result("recommend_destinations", "destinations", payload)
        return payload

    async def search_pois(self, destination: str) -> dict[str, Any]:
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
        self._result("search_pois", "pois", payload)
        return payload

    async def search_rail(
        self,
        origin: str,
        destination: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
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
        self._result("search_rail_options", "rail", payload)
        return payload

    async def search_hotels(self) -> list[dict[str, Any]]:
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
        self._result("search_hotels", "hotels", {"items": payload, "warning": warning})
        return payload

    async def get_weather(self) -> list[dict[str, Any]]:
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
        self._result("get_weather", "weather", payload)
        return payload

    def record_error(self, name: str, error: Exception) -> dict[str, Any]:
        """把工具错误转换为可展示结果，不让模型把失败描述成成功。"""

        code = getattr(error, "code", None)
        message = getattr(error, "message", None)
        retryable = getattr(error, "retryable", None)
        detail = {
            "code": code if isinstance(code, str) else "tool_unavailable",
            "message": message if isinstance(message, str) else str(error) or "工具暂时不可用",
            "retryable": retryable if isinstance(retryable, bool) else False,
        }
        payload = {"error": detail}
        self._result(name, "error", payload)
        return payload

    def _stamp(self, key: str, source: str) -> None:
        metadata = dict(self.snapshot.fact_metadata)
        metadata[key] = FactStamp(source=source, queried_at=datetime.now(timezone.utc))
        self.snapshot.fact_metadata = metadata

    def _result(self, name: str, artifact: str, data: Any) -> None:
        self.events.append(("tool.result", {"name": name, "artifact": artifact, "data": data}))
