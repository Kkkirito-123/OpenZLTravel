"""市内路线的确定性选择与降级。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol

from domain.models import City, Poi, RouteSegment

from .base import ProviderError
from .geo import local_route, local_routes


class RouteClient(Protocol):
    """实时路线供应商需要的最小异步接口。"""

    async def get_route_with_waypoints(self, day_pois: Sequence[Poi]) -> RouteSegment:
        """返回按顺序途经当日 POI 的真实驾车路线。"""

    async def get_transit(self, city: City, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """返回两个 POI 之间的真实公交路线。"""


class RouteProvider:
    """普通出行使用本地估算，仅显式实时模式调用高德。"""

    def __init__(self, realtime: RouteClient | None = None) -> None:
        self.realtime = realtime

    async def get_routes(
        self,
        city: City,
        day_pois: Sequence[Poi],
        mode: str,
    ) -> tuple[list[RouteSegment], list[str]]:
        """返回当日路线和去重警告，降级路线始终标记为估算。"""

        pois = list(day_pois)
        if len(pois) < 2:
            return [], []
        if mode in {"walk", "driving", "auto"}:
            return local_routes(pois, mode), []
        if mode == "realtime_driving":
            return await self._realtime_driving(pois)
        return await self._transit(city, pois)

    async def _realtime_driving(
        self, day_pois: list[Poi]
    ) -> tuple[list[RouteSegment], list[str]]:
        if self.realtime is None:
            return local_routes(day_pois, "driving"), ["实时驾车未配置，当前为本地估算。"]
        try:
            route = await self.realtime.get_route_with_waypoints(day_pois)
        except ProviderError:
            return local_routes(day_pois, "driving"), ["实时驾车不可用，当前为本地估算。"]
        return [route], []

    async def _transit(
        self, city: City, day_pois: list[Poi]
    ) -> tuple[list[RouteSegment], list[str]]:
        pairs = list(zip(day_pois, day_pois[1:], strict=False))
        if self.realtime is None:
            return [local_route(left, right, "walk") for left, right in pairs], [
                "公交路线未配置，当前为本地步行估算。"
            ]
        results = await asyncio.gather(
            *(self.realtime.get_transit(city, left, right) for left, right in pairs),
            return_exceptions=True,
        )
        routes: list[RouteSegment] = []
        degraded = False
        for (left, right), result in zip(pairs, results, strict=True):
            if isinstance(result, BaseException):
                routes.append(local_route(left, right, "walk"))
                degraded = True
            else:
                routes.append(result)
        warnings = ["部分公交路线不可用，已改为本地步行估算。"] if degraded else []
        return routes, warnings
