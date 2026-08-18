"""高德 Web Service 适配器。

本文件是唯一直接访问高德 HTTP API 的位置。它负责缓存、限流、GCJ-02/WGS-84 边界转换
和响应解析；不包含任务调度或本地目录优先级策略。
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any, cast

import httpx

from app.config import Settings
from app.errors import ProviderError
from app.models import (
    CandidateCatalog,
    City,
    Coordinate,
    DataSource,
    Poi,
    RouteSegment,
    TransitLine,
    WeatherDay,
)
from app.providers.base import CacheStore
from app.providers.geo import (
    amap_location,
    dict_items,
    first_mapping,
    mapping,
    now,
    parse_location,
    parse_polyline,
    photo_url,
    route_metrics,
    text,
)


class JsonResponseCache:
    """兼容独立测试的 JSON 高德缓存；生产主链路使用 Redis。"""

    def __init__(self, path: str, ttl_seconds: int) -> None:
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        self.values = self._load()

    def get(self, key: str) -> dict[str, Any] | None:
        """读取未过期的响应；缓存损坏时按未命中处理。"""

        item = self.values.get(key)
        if not isinstance(item, dict):
            return None
        saved_at = item.get("saved_at")
        payload = item.get("payload")
        if not isinstance(saved_at, (int, float)) or not isinstance(payload, dict):
            return None
        ttl_seconds = item.get("ttl_seconds", self.ttl_seconds)
        if not isinstance(ttl_seconds, (int, float)):
            ttl_seconds = self.ttl_seconds
        if time.time() - saved_at > ttl_seconds:
            return None
        return cast(dict[str, Any], payload)

    def set(
        self,
        key: str,
        payload: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        """原子写入成功响应，避免写到一半留下不可读文件。"""

        self.values[key] = {
            "saved_at": time.time(),
            "ttl_seconds": ttl_seconds or self.ttl_seconds,
            "payload": payload,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
            temporary.write_text(json.dumps(self.values, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            # 缓存只是性能优化，磁盘不可写时不能阻断行程生成。
            return

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}


class StoreResponseCache:
    """把高德原始响应放入统一 Redis 缓存。"""

    def __init__(self, store: CacheStore, provider: str = "amap") -> None:
        self.store = store
        self.provider = provider

    def get(self, key: str) -> dict[str, Any] | None:
        """读取尚未过期且结构完整的响应。"""

        value = self.store.get_cache(self.provider, key)
        return cast(dict[str, Any], value) if isinstance(value, dict) else None

    def set(self, key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        """按接口新鲜度写入 Redis 缓存。"""

        self.store.set_cache(self.provider, key, payload, ttl_seconds)


class AmapClient:
    """高德 Web 服务 API 的最小封装。"""

    def __init__(self, settings: Settings, cache_store: CacheStore | None = None) -> None:
        self.settings = settings
        self.cache = (
            StoreResponseCache(cache_store)
            if cache_store is not None
            else JsonResponseCache(
                settings.amap_cache_path,
                settings.amap_cache_ttl_seconds,
            )
        )
        self.rate_limited_until = 0.0
        self.last_request_at = 0.0
        self.request_lock = Lock()
        self.http = httpx.Client(
            base_url=settings.amap_base_url,
            timeout=settings.amap_timeout_seconds,
        )

    def close(self) -> None:
        """关闭同步 HTTP 客户端，避免热重载长期保留连接池。"""

        self.http.close()

    def _get(self, path: str, **params: str) -> dict[str, Any]:
        cache_key = cache_key_for(path, params)
        cached = self._cached_or_available(cache_key)
        if cached is not None:
            return cached
        if not self.settings.amap_api_key:
            raise ProviderError("amap_not_configured", "尚未配置高德地图 API Key")
        with self.request_lock:
            cached = self._cached_or_available(cache_key)
            if cached is not None:
                return cached
            self._wait_for_request_slot()
            payload = self._request_payload(path, params)
            if payload.get("status") != "1":
                self._raise_for_provider_error(payload)
            self.cache.set(cache_key, payload, amap_cache_ttl(path, self.settings))
            return payload

    def resolve_city(self, destination: str) -> City:
        """通过地理编码确认目的地城市和行政区编码。"""

        payload = self._get("/geocode/geo", address=destination)
        geocode = first_mapping(payload.get("geocodes"))
        if not geocode:
            raise ProviderError("city_not_found", f"无法确认目的地“{destination}”")
        name = text(geocode.get("city")) or text(geocode.get("district")) or destination
        latitude, longitude = parse_location(geocode.get("location"), from_amap=True)
        return City(
            name=name,
            adcode=geocode.get("adcode"),
            latitude=latitude,
            longitude=longitude,
        )

    def search_candidates(self, city: City) -> CandidateCatalog:
        """分别查询景点、餐厅和酒店，统一转成候选池。"""

        return CandidateCatalog(
            attractions=self._search(city, "风景名胜", "attraction"),
            restaurants=self._search(city, "餐饮服务", "restaurant"),
            hotels=self._search(city, "住宿服务", "hotel"),
        )

    def get_weather(self, city: City, start_date: date, end_date: date) -> list[WeatherDay]:
        """获取高德可提供的天气预报，超出范围的日期由应用层补充警告。"""

        payload = self._get(
            "/weather/weatherInfo",
            city=city.adcode or city.name,
            extensions="all",
        )
        forecasts = first_mapping(payload.get("forecasts")) or {}
        return [
            weather
            for item in dict_items(forecasts.get("casts"))
            if (weather := _amap_weather_day(item, start_date, end_date)) is not None
        ]

    def get_route(self, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """查询两个已知 POI 的驾车路线。"""

        payload = self._get(
            "/direction/driving",
            origin=amap_location(from_poi),
            destination=amap_location(to_poi),
            extensions="all",
        )
        path = first_mapping(mapping(payload.get("route")).get("paths"))
        if path is None:
            raise ProviderError("route_not_found", "无法获取两个景点之间的驾车路线")
        distance_km, duration_minutes = route_metrics(path)
        return RouteSegment(
            from_poi_id=from_poi.id,
            to_poi_id=to_poi.id,
            distance_km=distance_km,
            duration_minutes=duration_minutes,
            polyline=_driving_polyline(path),
            source=DataSource(provider="amap", freshness="realtime", fetched_at=now()),
        )

    def get_transit(self, city: City, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """解析高德公交/地铁方案；没有方案时抛出稳定错误。"""

        payload = self._get(
            "/direction/transit/integrated",
            origin=amap_location(from_poi),
            destination=amap_location(to_poi),
            city=city.adcode or city.name,
            strategy="0",
        )
        transit = first_mapping(mapping(payload.get("route")).get("transits"))
        if transit is None:
            raise ProviderError("route_not_found", "无法获取两个景点之间的公交或地铁路线")
        distance_km, duration_minutes = route_metrics(transit)
        return RouteSegment(
            from_poi_id=from_poi.id,
            to_poi_id=to_poi.id,
            distance_km=distance_km,
            duration_minutes=duration_minutes,
            mode="公交 / 地铁",
            polyline=_transit_polyline(transit),
            source=DataSource(provider="amap", freshness="realtime", fetched_at=now()),
            transit_lines=_transit_lines(transit),
        )

    def get_route_with_waypoints(self, day_pois: Sequence[Poi]) -> RouteSegment:
        """一天只发起一次驾车请求，并把中间景点作为有序途经点。"""

        if len(day_pois) < 2:
            raise ProviderError("route_not_found", "至少需要两个景点才能规划路线")
        payload = self._get(
            "/direction/driving",
            origin=amap_location(day_pois[0]),
            destination=amap_location(day_pois[-1]),
            waypoints=";".join(amap_location(poi) for poi in day_pois[1:-1]),
            extensions="all",
        )
        path = first_mapping(mapping(payload.get("route")).get("paths"))
        if path is None:
            raise ProviderError("route_not_found", "无法获取当天的实时驾车路线")
        distance_km, duration_minutes = route_metrics(path)
        return RouteSegment(
            from_poi_id=day_pois[0].id,
            to_poi_id=day_pois[-1].id,
            via_poi_ids=[poi.id for poi in day_pois[1:-1]],
            distance_km=distance_km,
            duration_minutes=duration_minutes,
            mode="实时驾车",
            polyline=_driving_polyline(path),
            source=DataSource(provider="amap", freshness="realtime", fetched_at=now()),
        )

    def _cached_or_available(self, cache_key: str) -> dict[str, Any] | None:
        """优先命中缓存；未命中且处于冷却期时快速失败。"""

        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        if time.monotonic() < self.rate_limited_until:
            raise ProviderError(
                "amap_rate_limited",
                "高德接口刚刚触发了频率限制，请稍后再试；已缓存的数据会优先使用。",
            )
        return None

    def _request_payload(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """执行一次 HTTP 调用，并在边界处拒绝非对象 JSON 响应。"""

        try:
            response = self.http.get(
                path,
                params={**params, "key": self.settings.amap_api_key},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("amap_unavailable", "高德地图服务暂时不可用") from exc
        if not isinstance(payload, dict):
            raise ProviderError("amap_invalid_response", "高德地图返回了无法识别的数据")
        return payload

    def _wait_for_request_slot(self) -> None:
        """串行控制请求间隔，避免天气和多段路线一起触发 QPS 限制。"""

        elapsed = time.monotonic() - self.last_request_at
        remaining = self.settings.amap_min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self.last_request_at = time.monotonic()

    def _raise_for_provider_error(self, payload: dict[str, Any]) -> None:
        info = text(payload.get("info"))
        if not is_rate_limited(info, payload.get("infocode")):
            raise ProviderError("amap_request_failed", "高德地图请求失败，请稍后再试")
        cooldown = max(1, round(self.settings.amap_rate_limit_cooldown_seconds))
        self.rate_limited_until = time.monotonic() + self.settings.amap_rate_limit_cooldown_seconds
        raise ProviderError(
            "amap_rate_limited",
            f"高德接口当前触发了频率限制，请等待约 {cooldown} 秒后再试；已缓存的数据会优先使用。",
        )

    def _search(self, city: City, keyword: str, category: str) -> list[Poi]:
        payload = self._get(
            "/place/text",
            keywords=keyword,
            city=city.adcode or city.name,
            citylimit="true",
            offset="8",
            extensions="all",
        )
        return [
            poi
            for raw in dict_items(payload.get("pois"))
            if (poi := _parse_poi(raw, category)) is not None
        ]


def cache_key_for(path: str, params: dict[str, str]) -> str:
    """只用接口和业务参数生成键，绝不把 API Key 写入缓存。"""

    return json.dumps(
        {"path": path, "params": sorted(params.items())},
        ensure_ascii=False,
        sort_keys=True,
    )


def amap_cache_ttl(path: str, settings: Settings) -> int:
    """实时路线使用短缓存，静态地点使用长缓存，避免陈旧数据冒充实时结果。"""

    ttl_by_path = {
        "/direction/driving": 5 * 60,
        "/direction/transit/integrated": 30 * 60,
        "/weather/weatherInfo": 30 * 60,
    }
    return ttl_by_path.get(path, settings.amap_cache_ttl_seconds)


def is_rate_limited(info: str, infocode: Any) -> bool:
    """识别高德限流文案，避免把内部英文错误直接展示给用户。"""

    normalized = info.upper()
    return "CUQPS" in normalized or "QPS" in normalized or str(infocode) == "10021"


def _amap_weather_day(item: dict[str, Any], start_date: date, end_date: date) -> WeatherDay | None:
    """忽略单条异常预报，避免一个脏日期破坏可用的其他天气数据。"""

    try:
        forecast_date = date.fromisoformat(str(item.get("date")))
    except (TypeError, ValueError):
        return None
    if not start_date <= forecast_date <= end_date:
        return None
    return WeatherDay(
        date=forecast_date,
        day_weather=text(item.get("dayweather")) or None,
        night_weather=text(item.get("nightweather")) or None,
        day_temperature=text(item.get("daytemp")) or None,
        night_temperature=text(item.get("nighttemp")) or None,
        source=DataSource(provider="amap", freshness="forecast", fetched_at=now()),
    )


def _parse_poi(raw: dict[str, Any], category: str) -> Poi | None:
    latitude, longitude = parse_location(raw.get("location"), from_amap=True)
    identifier = text(raw.get("id"))
    name = text(raw.get("name"))
    if latitude is None or longitude is None or not identifier or not name:
        return None
    return Poi(
        id=identifier,
        name=name,
        address=text(raw.get("address")),
        category=category,  # type: ignore[arg-type]
        latitude=latitude,
        longitude=longitude,
        type_name=text(raw.get("type")),
        image_url=photo_url(raw.get("photos")),
    )


def _driving_polyline(path: dict[str, Any]) -> list[Coordinate]:
    return [
        coordinate
        for step in dict_items(path.get("steps"))
        for coordinate in parse_polyline(step.get("polyline", ""))
    ]


def _transit_lines(transit: dict[str, Any]) -> list[TransitLine]:
    lines: list[TransitLine] = []
    for segment in dict_items(transit.get("segments")):
        bus = mapping(segment.get("bus"))
        for busline in dict_items(bus.get("buslines")):
            departure = mapping(busline.get("departure_stop"))
            arrival = mapping(busline.get("arrival_stop"))
            lines.append(
                TransitLine(
                    name=text(busline.get("name")) or "未知线路",
                    type=text(busline.get("type")) or "公交",
                    departure_stop=text(departure.get("name")),
                    arrival_stop=text(arrival.get("name")),
                    via_stops=[
                        text(stop.get("name"))
                        for stop in dict_items(busline.get("via_stops"))
                        if text(stop.get("name"))
                    ],
                )
            )
    return lines


def _transit_polyline(transit: dict[str, Any]) -> list[Coordinate]:
    values: list[Coordinate] = []
    for segment in dict_items(transit.get("segments")):
        walk = mapping(segment.get("walking"))
        values.extend(parse_polyline(text(walk.get("polyline"))))
        bus = mapping(segment.get("bus"))
        for busline in dict_items(bus.get("buslines")):
            values.extend(parse_polyline(text(busline.get("polyline"))))
    return values
