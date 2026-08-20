"""高德 Web Service 的异步事实边界。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from datetime import date
from typing import Any, Literal, cast

import httpx

from domain.models import CandidateCatalog, City, Poi, RouteSegment, WeatherDay

from .base import ProviderError, ProviderRuntime, stable_fact_id, stable_key
from .geo import (
    amap_location,
    dict_items,
    first_mapping,
    http_url,
    mapping,
    parse_location,
    parse_polyline,
    route_metrics,
    text,
)

PoiCategory = Literal["attraction", "restaurant", "hotel"]


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

        payload = await self._get("/geocode/geo", {"address": destination}, ttl_seconds=86400)
        geocode = first_mapping(payload.get("geocodes"))
        if geocode is None:
            raise ProviderError("city_not_found", f"无法确认目的地“{destination}”")
        location = parse_location(geocode.get("location"), from_amap=True)
        if location is None:
            raise ProviderError("city_not_found", "高德城市结果缺少有效坐标")
        latitude, longitude = location
        return City(
            name=text(geocode.get("city")) or text(geocode.get("district")) or destination,
            adcode=text(geocode.get("adcode")) or None,
            latitude=latitude,
            longitude=longitude,
        )

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
        self, city: City, catalog: CandidateCatalog, *, limit: int = 16
    ) -> CandidateCatalog:
        """给本地 OSM 目录补充缺失的地址和图片。

        本地目录的坐标和稳定 ID 是权威事实，但 OSM 中不少地点没有 ``addr:*``
        或 ``image`` 标签。这里只针对缺少展示信息的少量候选调用高德关键词搜索，
        找到同名地点后补齐地址和图片；匹配失败或网络失败时保留原始目录，绝不
        用猜测内容覆盖真实字段。
        """

        # 酒店选择卡片最依赖地址和图片，而且本地 Catalog 的 ``all`` 顺序是
        # 景点 → 餐饮 → 酒店。如果直接截取前 ``limit`` 个，酒店很容易永远
        # 排在队列之外。因此先处理酒店，再处理景点和餐饮。
        missing = sorted(
            (item for item in catalog.all if not item.address or not item.image_url),
            key=lambda item: (item.category != "hotel", item.name),
        )[: max(0, limit)]
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
            match = _best_poi_match(item, candidates)
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
            if (poi := _poi(item, category)) is not None
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
    """在高德 HTTP 边界把 Catalog 的 12 位国标 adcode 转为高德使用的 6 位编码。

    领域层保留完整行政区编码，避免丢失层级信息；只有请求高德天气、POI 或公交接口时
    才截取前 6 位。没有编码时继续使用城市名称，不能在领域模型中原地修改事实。
    """

    return city.adcode[:6] if city.adcode else city.name


def _poi(item: dict[str, Any], category: PoiCategory) -> Poi | None:
    location = parse_location(item.get("location"), from_amap=True)
    raw_id, name = text(item.get("id")), text(item.get("name"))
    if location is None or not raw_id or not name:
        return None
    latitude, longitude = location
    type_name = text(item.get("type"))
    return Poi(
        id=stable_fact_id("poi-amap", raw_id),
        name=name,
        address=text(item.get("address")),
        category=category,
        latitude=latitude,
        longitude=longitude,
        type_name=type_name,
        image_url=http_url(item.get("photos")),
        tags=[value for value in type_name.split(";") if value],
    )


def _best_poi_match(target: Poi, candidates: list[Poi]) -> Poi | None:
    """在高德结果中选择同类、同名或坐标最近的候选。"""

    same_category = [item for item in candidates if item.category == target.category]
    if not same_category:
        return None
    target_name = _normalize_name(target.name)
    exact = [item for item in same_category if _normalize_name(item.name) == target_name]
    if exact:
        return exact[0]
    named = [
        item
        for item in same_category
        if target_name in _normalize_name(item.name)
        or _normalize_name(item.name) in target_name
    ]
    if named:
        return min(named, key=lambda item: _distance(target, item))
    nearest = min(same_category, key=lambda item: _distance(target, item))
    return nearest if _distance(target, nearest) <= 0.03 else None


def _normalize_name(value: str) -> str:
    """去掉常见行政区和酒店后缀，提升跨 Provider 名称匹配率。"""

    normalized = re.sub(r"[市区县酒店宾馆旅馆度假村]+$", "", value.strip())
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", normalized).casefold()


def _distance(left: Poi, right: Poi) -> float:
    """使用经纬度平方距离做本地候选排序，不把它当作路线距离。"""

    return (left.latitude - right.latitude) ** 2 + (left.longitude - right.longitude) ** 2


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
