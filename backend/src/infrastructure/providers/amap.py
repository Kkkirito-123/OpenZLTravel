"""高德 Web Service 的异步事实边界。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date
from typing import Any, Literal, cast

import httpx

from domain.models import (
    CandidateCatalog,
    City,
    Poi,
    ResolvedPlace,
    RouteSegment,
    WeatherDay,
)

from .amap_matching import (
    best_poi_match,
    best_query_match,
    city_from_geocode,
    is_administrative_query,
    poi_from_item,
)
from .base import ProviderError, ProviderRuntime, stable_key
from .geo import (
    amap_location,
    dict_items,
    first_mapping,
    mapping,
    parse_polyline,
    route_metrics,
    text,
)

PoiCategory = Literal["attraction", "restaurant", "hotel"]


def _enrichment_candidates(catalog: CandidateCatalog, limit: int) -> list[Poi]:
    """按首屏配额选择需要补充展示字段的 POI。"""

    candidates = [item for item in catalog.all if not item.address or not item.image_url]
    if limit <= 4:
        return sorted(
            candidates,
            key=lambda item: (item.category != "hotel", item.name),
        )[: max(0, limit)]

    categories = ("attraction", "hotel", "restaurant")
    by_category = {
        category: sorted(
            (item for item in candidates if item.category == category),
            key=lambda item: item.name,
        )
        for category in categories
    }
    quotas = {
        "attraction": min(12, limit),
        "hotel": min(8, max(0, limit - 12)),
        "restaurant": min(4, max(0, limit - 20)),
    }
    selected = [
        item
        for category in categories
        for item in by_category[category][: quotas[category]]
    ]
    selected_ids = {item.id for item in selected}
    if len(selected) < limit:
        selected.extend(
            item
            for item in sorted(candidates, key=lambda value: (value.category, value.name))
            if item.id not in selected_ids
        )
    return selected[:limit]


class AmapClient:
    """使用共享异步连接池读取高德城市、POI、天气与路线事实。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://restapi.amap.com/v3",
        timeout_seconds: float = 5,
        concurrency: int = 2,
        runtime: ProviderRuntime | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.runtime = runtime or ProviderRuntime(
            "amap", timeout_seconds=timeout_seconds, concurrency=concurrency
        )
        self.http = http or httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds)
        self._owns_http = http is None

    async def resolve_city(self, destination: str) -> City:
        """用地理编码确认城市和 WGS-84 中心点。"""

        geocode = await self._geocode(destination)
        return city_from_geocode(destination, geocode)

    async def resolve_place(self, query: str) -> ResolvedPlace:
        """区分规范城市和具体地点；非行政区输入必须命中 POI，不能静默当成城市。"""

        geocode = await self._geocode(query)
        city = city_from_geocode(query, geocode)
        if is_administrative_query(query, geocode):
            return ResolvedPlace(query=query, city=city)
        candidates = await self._search(city, query, "attraction", 5)
        poi = best_query_match(query, candidates)
        if poi is None:
            raise ProviderError("place_not_found", f"无法确认具体地点“{query}”")
        return ResolvedPlace(query=query, city=city, poi=poi)

    async def search_candidates(self, city: City) -> CandidateCatalog:
        """并行读取三类 POI；餐饮/酒店失败可为空，景点失败则拒绝规划。"""

        results = await asyncio.gather(
            self._search(city, "风景名胜", "attraction", 12),
            self._search(city, "餐饮服务", "restaurant", 12),
            self._search(city, "住宿服务", "hotel", 8),
            return_exceptions=True,
        )
        if isinstance(results[0], BaseException):
            raise ProviderError("catalog_not_found", f"未找到{city.name}的可用景点")
        attractions = cast(list[Poi], results[0])
        if not attractions:
            raise ProviderError("catalog_not_found", f"未找到{city.name}的可用景点")
        return CandidateCatalog(
            attractions=attractions,
            restaurants=(
                [] if isinstance(results[1], BaseException) else cast(list[Poi], results[1])
            ),
            hotels=[] if isinstance(results[2], BaseException) else cast(list[Poi], results[2]),
        )

    async def enrich_catalog(
        self, city: City, catalog: CandidateCatalog, *, limit: int = 24
    ) -> CandidateCatalog:
        """用高德同名匹配补齐首屏图片和地址；失败时保留原事实，不猜测。"""

        # 首屏工作台先展示景点，不能让酒店候选耗尽补图名额。默认配额覆盖
        # 桌面端景点首屏 12 张、酒店首屏 8 张和少量餐饮；实际不足时再由
        # 其他类别补足。显式传入很小的 limit 时保留旧的“酒店优先”行为，
        # 兼容只针对酒店展示字段的调用方。
        missing = _enrichment_candidates(catalog, limit)
        if not missing:
            return catalog

        # 高德关键词搜索有 QPS 限制。这里宁可顺序补充少量展示字段，也不让
        # 并发请求把整批补充都打成 ``amap_rate_limited``。
        semaphore = asyncio.Semaphore(1)
        request_lock = asyncio.Lock()
        last_request_at = 0.0

        async def enrich(item: Poi) -> Poi:
            nonlocal last_request_at
            async with semaphore:
                try:
                    async with request_lock:
                        now = asyncio.get_running_loop().time()
                        wait_seconds = 0.35 - (now - last_request_at)
                        if wait_seconds > 0:
                            await asyncio.sleep(wait_seconds)
                        last_request_at = asyncio.get_running_loop().time()
                        candidates = await self._search(city, item.name, item.category, 5)
                except Exception:
                    return item
            match = best_poi_match(item, candidates)
            if match is None:
                return item
            return item.model_copy(
                update={
                    "address": item.address or match.address,
                    "image_url": item.image_url or match.image_url,
                }
            )

        enriched = await asyncio.gather(*(enrich(item) for item in missing))
        replacements = {item.id: item for item in enriched}

        def replace(items: list[Poi]) -> list[Poi]:
            return [replacements.get(item.id, item) for item in items]

        return catalog.model_copy(
            update={
                "attractions": replace(catalog.attractions),
                "restaurants": replace(catalog.restaurants),
                "hotels": replace(catalog.hotels),
            }
        )

    async def get_weather(
        self, city: City, start_date: date, end_date: date
    ) -> list[WeatherDay]:
        """读取高德实际覆盖的预报日期，不填补超出范围的天气。"""

        payload = await self._get(
            "/weather/weatherInfo",
            {"city": _amap_city(city), "extensions": "all"},
            ttl_seconds=1800,
        )
        forecast = first_mapping(payload.get("forecasts")) or {}
        return [
            weather
            for item in dict_items(forecast.get("casts"))
            if (weather := _weather_day(item, start_date, end_date)) is not None
        ]

    async def get_route(self, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """读取两个真实 POI 之间的高德驾车路线。"""

        payload = await self._get(
            "/direction/driving",
            {"origin": amap_location(from_poi), "destination": amap_location(to_poi)},
            ttl_seconds=300,
        )
        path = first_mapping(mapping(payload.get("route")).get("paths"))
        if path is None:
            raise ProviderError("route_not_found", "高德未返回可用驾车路线")
        return _route_segment(from_poi, to_poi, path, "实时驾车")

    async def get_route_with_waypoints(self, day_pois: Sequence[Poi]) -> RouteSegment:
        """用一次请求查询带途经点的当日驾车路线。"""

        if len(day_pois) < 2:
            raise ProviderError("route_not_found", "至少需要两个 POI 才能查询路线")
        params = {
            "origin": amap_location(day_pois[0]),
            "destination": amap_location(day_pois[-1]),
            "waypoints": ";".join(amap_location(item) for item in day_pois[1:-1]),
        }
        payload = await self._get("/direction/driving", params, ttl_seconds=300)
        path = first_mapping(mapping(payload.get("route")).get("paths"))
        if path is None:
            raise ProviderError("route_not_found", "高德未返回可用驾车路线")
        return _route_segment(day_pois[0], day_pois[-1], path, "实时驾车")

    async def get_transit(self, city: City, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """读取两个 POI 之间的公交/地铁路线。"""

        payload = await self._get(
            "/direction/transit/integrated",
            {
                "origin": amap_location(from_poi),
                "destination": amap_location(to_poi),
                "city": _amap_city(city),
                "strategy": "0",
            },
            ttl_seconds=1800,
        )
        transit = first_mapping(mapping(payload.get("route")).get("transits"))
        if transit is None:
            raise ProviderError("route_not_found", "高德未返回可用公交路线")
        distance, duration = route_metrics(transit)
        return RouteSegment(
            from_poi_id=from_poi.id,
            to_poi_id=to_poi.id,
            distance_km=distance,
            duration_minutes=duration,
            mode="公交 / 地铁",
            polyline=_transit_polyline(transit),
            source="amap",
        )

    async def aclose(self) -> None:
        """关闭本实例创建的异步 HTTP 连接池。"""

        if self._owns_http:
            await self.http.aclose()

    async def _geocode(self, query: str) -> dict[str, Any]:
        """读取单个地理编码结果，供城市与地点解析共享。"""

        payload = await self._get("/geocode/geo", {"address": query}, ttl_seconds=86400)
        geocode = first_mapping(payload.get("geocodes"))
        if geocode is None:
            raise ProviderError("place_not_found", f"无法确认地点“{query}”")
        return geocode

    async def _search(
        self, city: City, keyword: str, category: PoiCategory, limit: int
    ) -> list[Poi]:
        payload = await self._get(
            "/place/text",
            {
                "keywords": keyword,
                "city": _amap_city(city),
                "citylimit": "true",
                "offset": str(limit),
                "extensions": "all",
            },
            ttl_seconds=86400,
        )
        return [
            poi
            for item in dict_items(payload.get("pois"))
            if (poi := poi_from_item(item, category)) is not None
        ]

    async def _get(
        self, path: str, params: dict[str, str], *, ttl_seconds: int
    ) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderError("amap_not_configured", "尚未配置高德地图 API Key")
        key = stable_key(path, sorted(params.items()))

        async def request() -> dict[str, Any]:
            """执行一次真实请求；API Key 只在发送边界加入，绝不进入缓存键。"""

            response = await self.http.get(path, params={**params, "key": self.api_key})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("amap response must be object")
            if payload.get("status") != "1":
                _raise_amap_error(payload)
            return cast(dict[str, Any], payload)

        payload, _ = await self.runtime.run(key, request, ttl_seconds=ttl_seconds)
        return payload


def _amap_city(city: City) -> str:
    """仅在高德 HTTP 边界把 12 位 adcode 转为 6 位；领域事实保持不变。"""

    return city.adcode[:6] if city.adcode else city.name


def _weather_day(
    item: dict[str, Any], start_date: date, end_date: date
) -> WeatherDay | None:
    try:
        item_date = date.fromisoformat(text(item.get("date")))
    except ValueError:
        return None
    if not start_date <= item_date <= end_date:
        return None
    return WeatherDay(
        date=item_date,
        day_weather=text(item.get("dayweather")) or None,
        night_weather=text(item.get("nightweather")) or None,
        day_temperature=text(item.get("daytemp")) or None,
        night_temperature=text(item.get("nighttemp")) or None,
        source="amap",
    )


def _route_segment(left: Poi, right: Poi, path: dict[str, Any], mode: str) -> RouteSegment:
    distance, duration = route_metrics(path)
    polyline = [
        point
        for step in dict_items(path.get("steps"))
        for point in parse_polyline(step.get("polyline"))
    ]
    return RouteSegment(
        from_poi_id=left.id,
        to_poi_id=right.id,
        distance_km=distance,
        duration_minutes=duration,
        mode=mode,
        polyline=polyline,
        source="amap",
    )


def _transit_polyline(transit: dict[str, Any]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for segment in dict_items(transit.get("segments")):
        points.extend(parse_polyline(mapping(segment.get("walking")).get("polyline")))
        for line in dict_items(mapping(segment.get("bus")).get("buslines")):
            points.extend(parse_polyline(line.get("polyline")))
    return points


def _raise_amap_error(payload: dict[str, Any]) -> None:
    info = text(payload.get("info")).upper()
    infocode = text(payload.get("infocode"))
    if "QPS" in info or infocode == "10021":
        raise ProviderError("amap_rate_limited", "高德地图请求过于频繁")
    raise ProviderError("amap_request_failed", "高德地图请求失败")
