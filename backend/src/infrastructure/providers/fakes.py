"""离线图测试可直接注入的 Fake Provider。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Literal

from domain.models import (
    CandidateCatalog,
    City,
    DestinationCandidate,
    HotelOption,
    Poi,
    RailOption,
    RailSeat,
    ResolvedPlace,
    RouteSegment,
    TravelRequirements,
    WeatherDay,
)

from .base import ProviderError, stable_fact_id
from .geo import local_routes


class FakeCatalogProvider:
    """返回预置城市、POI 和目的地候选。"""

    def __init__(
        self,
        city: City,
        catalog: CandidateCatalog,
        destinations: list[DestinationCandidate] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.city = city
        self.catalog = catalog
        self.destinations = destinations or []
        self.fail = fail

    async def resolve_city(self, destination: str) -> City:
        """返回预置城市。"""

        self._ensure_available()
        return self.city

    async def resolve_place(self, query: str) -> ResolvedPlace:
        """默认把查询视为规范城市；需要 POI 的测试可注入专用 Gateway。"""

        self._ensure_available()
        return ResolvedPlace(query=query, city=self.city)

    async def search_candidates(self, city: City) -> CandidateCatalog:
        """返回预置 POI 候选池。"""

        self._ensure_available()
        return self.catalog

    async def recommend_destinations(
        self,
        origin: str,
        region: str,
        preferences: list[str],
        limit: int = 5,
    ) -> list[DestinationCandidate]:
        """按已给顺序返回最多 ``limit`` 个候选。"""

        self._ensure_available()
        return self.destinations[: min(5, max(1, limit))]

    def _ensure_available(self) -> None:
        if self.fail:
            raise ProviderError("fake_catalog_failed", "Fake 地点目录按测试设置失败")


class FakeRailProvider:
    """按去程/返程返回预置车次。"""

    def __init__(
        self,
        outbound: list[RailOption] | None = None,
        returning: list[RailOption] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.outbound = outbound
        self.returning = returning
        self.fail = fail

    async def search(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        direction: str,
    ) -> tuple[list[RailOption], bool]:
        """返回预置车次，Fake 结果永不标记缓存命中。"""

        if self.fail:
            raise ProviderError("fake_rail_failed", "Fake 铁路 Provider 按测试设置失败")
        configured = self.outbound if direction == "outbound" else self.returning
        if configured is not None:
            return configured, False
        return [_fake_rail_option(origin, destination, travel_date, direction)], False


class FakeHotelProvider:
    """返回预置酒店或模拟降级。"""

    def __init__(
        self,
        options: list[HotelOption] | None = None,
        *,
        warning: str | None = None,
        fail: bool = False,
    ) -> None:
        self.options = options or []
        self.warning = warning
        self.fail = fail

    async def search(
        self, requirements: TravelRequirements, catalog: CandidateCatalog
    ) -> tuple[list[HotelOption], bool, str | None]:
        """返回预置酒店、缓存状态和可选警告。"""

        if self.fail:
            raise ProviderError("fake_hotel_failed", "Fake 酒店 Provider 按测试设置失败")
        return self.options, False, self.warning


class FakeWeatherProvider:
    """返回预置天气事实。"""

    def __init__(self, weather: list[WeatherDay] | None = None, *, fail: bool = False) -> None:
        self.weather = weather
        self.fail = fail

    async def get_weather(
        self, city: City, start_date: date, end_date: date
    ) -> list[WeatherDay]:
        """返回预置天气。"""

        if self.fail:
            raise ProviderError("fake_weather_failed", "Fake 天气 Provider 按测试设置失败")
        if self.weather is not None:
            return self.weather
        return [
            WeatherDay(
                date=date.fromordinal(ordinal),
                warning="Fake Provider 离线天气，不代表实时预报。",
                source="unknown",
            )
            for ordinal in range(start_date.toordinal(), end_date.toordinal() + 1)
        ]


class FakeRouteProvider:
    """使用确定性本地估算返回路线。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def get_routes(
        self, city: City, day_pois: Sequence[Poi], mode: str
    ) -> tuple[list[RouteSegment], list[str]]:
        """返回离线路线，可按测试设置招致稳定失败。"""

        if self.fail:
            raise ProviderError("fake_route_failed", "Fake 路线 Provider 按测试设置失败")
        return local_routes(list(day_pois), mode), []


def _fake_rail_option(
    origin: str,
    destination: str,
    travel_date: date,
    direction: str,
) -> RailOption:
    """生成与请求日期一致的离线车次事实。"""

    normalized_direction: Literal["outbound", "return"] = (
        "return" if direction == "return" else "outbound"
    )
    from_station, to_station = (
        (destination, origin) if normalized_direction == "return" else (origin, destination)
    )
    return RailOption(
        option_id=stable_fact_id("rail-fake", normalized_direction, travel_date),
        direction=normalized_direction,
        travel_date=travel_date,
        train_code="G100",
        from_station=from_station,
        to_station=to_station,
        departure_time="09:00",
        arrival_time="10:30",
        duration_minutes=90,
        seats=[RailSeat(name="二等座", availability="有", price=100)],
        price_from=100,
        has_ticket=True,
    )
