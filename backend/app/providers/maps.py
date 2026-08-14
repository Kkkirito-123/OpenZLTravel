"""OpenZLTravel 的地图、天气与本地交通提供者。

本文件只处理地理事实、坐标转换和交通响应，不承担模型规划、会话或持久化职责。
"""

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

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
from app.providers.base import CacheStore, stable_key


class MapProvider(Protocol):
    """旅行服务依赖的地图与天气能力。"""

    def resolve_city(self, destination: str) -> City:
        """确认目的地城市。"""

        ...

    def search_candidates(self, city: City) -> CandidateCatalog:
        """返回城市内可供模型选择的真实 POI。"""

        ...

    def get_weather(
        self,
        city: City,
        start_date: date,
        end_date: date,
    ) -> list[WeatherDay]:
        """返回日期范围内供应商实际提供的天气。"""

        ...

    def get_route(self, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """返回两个真实 POI 之间的路线。"""

        ...


class WeatherProvider(Protocol):
    """天气供应商的最小同步接口，便于主来源和兜底来源替换。"""

    def get_weather(
        self, city: City, start_date: date, end_date: date
    ) -> list[WeatherDay]:
        """查询指定日期范围的天气。"""

        ...


@dataclass(frozen=True)
class TransportResult:
    """一日交通结果及无法使用实时服务时的解释性警告。"""

    routes: list[RouteSegment]
    warnings: list[str]


class CatalogReader(Protocol):
    """离线目录所需的最小读取能力，避免提供方依赖具体数据库实现。"""

    @property
    def available(self) -> bool:
        """返回本地目录是否存在。"""

        ...

    def resolve_city(self, destination: str) -> City:
        """根据城市名返回本地城市事实。"""

        ...

    def search_candidates(self, city: City) -> CandidateCatalog:
        """根据城市坐标返回本地 POI 候选。"""

        ...


class HybridMapProvider:
    """优先使用本地 POI，缺少覆盖时才调用高德发现数据。"""

    def __init__(
        self,
        catalog: CatalogReader,
        upstream: MapProvider,
        allow_amap_fallback: bool = True,
        weather_provider: WeatherProvider | None = None,
        scheduler: "AmapScheduler | None" = None,
    ) -> None:
        self.catalog = catalog
        self.upstream = upstream
        self.allow_amap_fallback = allow_amap_fallback
        self.weather_provider = weather_provider
        self.scheduler = scheduler or AmapScheduler()

    def resolve_city(self, destination: str) -> City:
        """优先从 GeoNames 城市索引解析目的地。"""

        if self.catalog.available:
            try:
                return self.catalog.resolve_city(destination)
            except LookupError as error:
                self._raise_without_fallback(error)
        return self.upstream.resolve_city(destination)

    async def resolve_city_async(self, destination: str) -> City:
        """通过调度器执行城市兜底查询，避免并行任务绕过高德限流控制。"""

        result = await self._scheduled(
            _transport_key("city", destination),
            lambda: self.resolve_city(destination),
        )
        return cast(City, result)

    def search_candidates(self, city: City) -> CandidateCatalog:
        """优先使用 OSM POI，避免重复调用高德地点搜索接口。"""

        if self.catalog.available:
            try:
                return self.catalog.search_candidates(city)
            except LookupError as error:
                self._raise_without_fallback(error)
        return self.upstream.search_candidates(city)

    async def search_candidates_async(self, city: City) -> CandidateCatalog:
        """通过调度器执行 POI 兜底查询；本地目录命中时不会产生高德请求。"""

        result = await self._scheduled(
            _transport_key("candidates", city.name),
            lambda: self.search_candidates(city),
        )
        return cast(CandidateCatalog, result)

    def get_weather(self, city: City, start_date: date, end_date: date) -> list[WeatherDay]:
        """优先查询 Open-Meteo，失败或日期不完整时再使用高德兜底。"""

        if self.weather_provider is not None:
            try:
                weather = self.weather_provider.get_weather(city, start_date, end_date)
                if _covers_dates(weather, start_date, end_date):
                    return weather
            except (ProviderError, httpx.HTTPError, ValueError):
                pass
        return self.upstream.get_weather(city, start_date, end_date)

    def get_route(self, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """路线仍使用高德驾车轨迹，OSM POI 不直接等同于驾车路线。"""

        return self.upstream.get_route(from_poi, to_poi)

    async def get_weather_async(
        self, city: City, start_date: date, end_date: date
    ) -> list[WeatherDay]:
        """异步天气查询；Open-Meteo 成功时完全跳过高德天气接口。"""

        if self.weather_provider is not None:
            try:
                weather = await asyncio.to_thread(
                    self.weather_provider.get_weather, city, start_date, end_date
                )
                if _covers_dates(weather, start_date, end_date):
                    return weather
            except (ProviderError, httpx.HTTPError, ValueError):
                pass
        key = _transport_key("weather", city.name, start_date.isoformat(), end_date.isoformat())
        result = await self._scheduled(
            key,
            lambda: self.upstream.get_weather(city, start_date, end_date),
        )
        return cast(list[WeatherDay], result)

    async def get_transport_async(
        self,
        city: City,
        day_pois: Sequence[Poi],
        mode: str,
    ) -> TransportResult:
        """按用户交通偏好选择本地估算、高德公交或高德实时驾车。"""

        if len(day_pois) < 2:
            return TransportResult([], [])
        if mode in {"walk", "driving", "auto"}:
            return TransportResult(_local_routes(day_pois, mode), [])
        if mode == "realtime_driving":
            return await self._realtime_driving(day_pois)
        return await self._transit(city, day_pois)

    async def _transit(self, city: City, day_pois: Sequence[Poi]) -> TransportResult:
        routes: list[RouteSegment] = []
        warnings: list[str] = []
        for left, right in zip(day_pois, day_pois[1:], strict=False):
            key = _transport_key("transit", city.name, left.id, right.id)
            try:
                def fetch_transit(
                    from_poi: Poi = left,
                    to_poi: Poi = right,
                ) -> RouteSegment:
                    return _call_route(self.upstream, "get_transit", city, from_poi, to_poi)

                route = cast(
                    RouteSegment,
                    await self._scheduled(
                        key,
                        fetch_transit,
                    ),
                )
                routes.append(route)
            except ProviderError:
                routes.append(_local_route(left, right, "walk"))
                warnings.append("公交路线暂时不可用，当前显示为本地步行估算。")
        return TransportResult(routes, _unique(warnings))

    async def _realtime_driving(self, day_pois: Sequence[Poi]) -> TransportResult:
        key = _transport_key("realtime_driving", *(poi.id for poi in day_pois))
        try:
            route = await self._scheduled(
                key,
                lambda: _call_route(self.upstream, "get_route_with_waypoints", day_pois),
            )
            return TransportResult([route], [])
        except ProviderError:
            return TransportResult(
                [_local_route(day_pois[0], day_pois[-1], "driving", day_pois[1:-1])],
                ["实时驾车路线暂时不可用，当前显示为本地驾车估算。"],
            )

    async def _scheduled(self, key: str, operation: Callable[[], Any]) -> Any:
        return await self.scheduler.run(key, lambda: asyncio.to_thread(operation))

    def _raise_without_fallback(self, error: LookupError) -> None:
        if not self.allow_amap_fallback:
            raise ProviderError("local_data_not_found", str(error)) from error


class AmapScheduler:
    """统一限制高德异步调用，并合并同一进程内的重复请求。"""

    def __init__(self, concurrency: int = 2, min_interval_seconds: float = 0.4) -> None:
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.min_interval_seconds = min_interval_seconds
        self.last_request_at = 0.0
        self.slot_lock = asyncio.Lock()
        self.inflight_lock = asyncio.Lock()
        self.inflight: dict[str, asyncio.Task[Any]] = {}

    async def run(self, key: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        """相同 key 共享任务，失败直接返回，不做危险的立即重试。"""

        async with self.inflight_lock:
            existing = self.inflight.get(key)
            if existing is None:
                existing = asyncio.create_task(self._execute(key, operation))
                self.inflight[key] = existing
        try:
            return await existing
        finally:
            async with self.inflight_lock:
                if self.inflight.get(key) is existing:
                    self.inflight.pop(key, None)

    async def _execute(self, key: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        del key
        async with self.semaphore:
            async with self.slot_lock:
                elapsed = time.monotonic() - self.last_request_at
                wait = self.min_interval_seconds - elapsed
                if wait > 0:
                    await asyncio.sleep(wait)
                self.last_request_at = time.monotonic()
            return await operation()


class JsonResponseCache:
    """兼容旧部署的 JSON 高德缓存；新应用主链路使用 SQLite。"""

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
            temporary.write_text(
                json.dumps(self.values, ensure_ascii=False),
                encoding="utf-8",
            )
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
    """把高德原始响应放入统一 Provider 缓存，避免 JSON 文件并发覆盖。"""

    def __init__(self, store: CacheStore, provider: str = "amap") -> None:
        self.store = store
        self.provider = provider

    def get(self, key: str) -> dict[str, Any] | None:
        """读取尚未过期且结构完整的响应。"""

        value = self.store.get_cache(self.provider, key)
        return cast(dict[str, Any], value) if isinstance(value, dict) else None

    def set(self, key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        """按接口新鲜度写入 SQLite 缓存。"""

        self.store.set_cache(self.provider, key, payload, ttl_seconds)


def _cache_key(path: str, params: dict[str, str]) -> str:
    """只用接口和业务参数生成键，绝不把 API Key 写入缓存。"""

    return json.dumps(
        {"path": path, "params": sorted(params.items())},
        ensure_ascii=False,
        sort_keys=True,
    )


def _amap_cache_ttl(path: str, settings: Settings) -> int:
    """实时路线使用短缓存，静态地点使用长缓存，避免陈旧数据冒充实时结果。"""

    ttl_by_path = {
        "/direction/driving": 5 * 60,
        "/direction/transit/integrated": 30 * 60,
        "/weather/weatherInfo": 30 * 60,
    }
    return ttl_by_path.get(path, settings.amap_cache_ttl_seconds)


def _is_rate_limited(info: str, infocode: Any) -> bool:
    """识别高德限流文案，避免把内部英文错误直接展示给用户。"""

    normalized = info.upper()
    return "CUQPS" in normalized or "QPS" in normalized or str(infocode) == "10021"


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

    def _get(self, path: str, **params: str) -> dict[str, Any]:
        cache_key = _cache_key(path, params)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        if time.monotonic() < self.rate_limited_until:
            raise ProviderError(
                "amap_rate_limited",
                "高德接口刚刚触发了频率限制，请稍后再试；已缓存的数据会优先使用。",
            )
        if not self.settings.amap_api_key:
            raise ProviderError("amap_not_configured", "尚未配置高德地图 API Key")
        with self.request_lock:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
            if time.monotonic() < self.rate_limited_until:
                raise ProviderError(
                    "amap_rate_limited",
                    "高德接口刚刚触发了频率限制，请稍后再试；已缓存的数据会优先使用。",
                )
            self._wait_for_request_slot()
            try:
                response = self.http.get(
                    path,
                    params={**params, "key": self.settings.amap_api_key},
                )
                response.raise_for_status()
                payload = cast(dict[str, Any], response.json())
            except (httpx.HTTPError, ValueError) as exc:
                raise ProviderError("amap_unavailable", "高德地图服务暂时不可用") from exc
            if payload.get("status") != "1":
                self._raise_for_provider_error(payload)
            self.cache.set(cache_key, payload, _amap_cache_ttl(path, self.settings))
            return payload

    def _wait_for_request_slot(self) -> None:
        """串行控制请求间隔，避免天气和多段路线一起触发 QPS 限制。"""

        elapsed = time.monotonic() - self.last_request_at
        remaining = self.settings.amap_min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self.last_request_at = time.monotonic()

    def _raise_for_provider_error(self, payload: dict[str, Any]) -> None:
        info = _text(payload.get("info"))
        if not _is_rate_limited(info, payload.get("infocode")):
            raise ProviderError("amap_request_failed", "高德地图请求失败，请稍后再试")
        cooldown = max(1, round(self.settings.amap_rate_limit_cooldown_seconds))
        self.rate_limited_until = time.monotonic() + self.settings.amap_rate_limit_cooldown_seconds
        raise ProviderError(
            "amap_rate_limited",
            f"高德接口当前触发了频率限制，请等待约 {cooldown} 秒后再试；"
            "已缓存的数据会优先使用。",
        )

    def resolve_city(self, destination: str) -> City:
        """通过地理编码确认目的地城市和行政区编码。"""

        payload = self._get("/geocode/geo", address=destination)
        geocode = _first(payload.get("geocodes"))
        if not geocode:
            raise ProviderError("city_not_found", f"无法确认目的地“{destination}”")
        name = _text(geocode.get("city")) or _text(geocode.get("district")) or destination
        latitude, longitude = _location(geocode.get("location"), from_amap=True)
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
            poi for raw in payload.get("pois", []) if (poi := _parse_poi(raw, category)) is not None
        ]

    def get_weather(self, city: City, start_date: date, end_date: date) -> list[WeatherDay]:
        """获取高德可提供的天气预报，超出范围的日期由应用层补充警告。"""

        payload = self._get(
            "/weather/weatherInfo",
            city=city.adcode or city.name,
            extensions="all",
        )
        forecasts = _first(payload.get("forecasts")) or {}
        return [
            WeatherDay(
                date=date.fromisoformat(item["date"]),
                day_weather=item.get("dayweather"),
                night_weather=item.get("nightweather"),
                day_temperature=item.get("daytemp"),
                night_temperature=item.get("nighttemp"),
                source=DataSource(provider="amap", freshness="forecast", fetched_at=_now()),
            )
            for item in forecasts.get("casts", [])
            if _in_range(item.get("date"), start_date, end_date)
        ]

    def get_route(self, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """查询两个已知 POI 的驾车路线。"""

        payload = self._get(
            "/direction/driving",
            origin=_amap_location(from_poi),
            destination=_amap_location(to_poi),
            extensions="all",
        )
        path = _first(payload.get("route", {}).get("paths"))
        if path is None:
            raise ProviderError("route_not_found", "无法获取两个景点之间的驾车路线")
        polyline = [
            coordinate
            for step in path.get("steps", [])
            for coordinate in _polyline(step.get("polyline", ""))
        ]
        return RouteSegment(
            from_poi_id=from_poi.id,
            to_poi_id=to_poi.id,
            distance_km=round(float(path.get("distance", 0)) / 1000, 2),
            duration_minutes=max(1, round(float(path.get("duration", 0)) / 60)),
            polyline=polyline,
            source=DataSource(provider="amap", freshness="realtime", fetched_at=_now()),
        )

    def get_transit(self, city: City, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """解析高德公交/地铁方案；没有方案时抛出稳定错误。"""

        payload = self._get(
            "/direction/transit/integrated",
            origin=_amap_location(from_poi),
            destination=_amap_location(to_poi),
            city=city.adcode or city.name,
            strategy="0",
        )
        route = payload.get("route", {})
        transits = route.get("transits", [])
        transit = _first(transits)
        if transit is None:
            raise ProviderError("route_not_found", "无法获取两个景点之间的公交或地铁路线")
        lines = _transit_lines(transit)
        return RouteSegment(
            from_poi_id=from_poi.id,
            to_poi_id=to_poi.id,
            distance_km=round(float(transit.get("distance", 0)) / 1000, 2),
            duration_minutes=max(1, round(float(transit.get("duration", 0)) / 60)),
            mode="公交 / 地铁",
            polyline=_transit_polyline(transit),
            source=DataSource(provider="amap", freshness="realtime", fetched_at=_now()),
            transit_lines=lines,
        )

    def get_route_with_waypoints(self, day_pois: Sequence[Poi]) -> RouteSegment:
        """一天只发起一次驾车请求，并把中间景点作为有序途经点。"""

        if len(day_pois) < 2:
            raise ProviderError("route_not_found", "至少需要两个景点才能规划路线")
        payload = self._get(
            "/direction/driving",
            origin=_amap_location(day_pois[0]),
            destination=_amap_location(day_pois[-1]),
            waypoints=";".join(_amap_location(poi) for poi in day_pois[1:-1]),
            extensions="all",
        )
        path = _first(payload.get("route", {}).get("paths"))
        if path is None:
            raise ProviderError("route_not_found", "无法获取当天的实时驾车路线")
        return RouteSegment(
            from_poi_id=day_pois[0].id,
            to_poi_id=day_pois[-1].id,
            via_poi_ids=[poi.id for poi in day_pois[1:-1]],
            distance_km=round(float(path.get("distance", 0)) / 1000, 2),
            duration_minutes=max(1, round(float(path.get("duration", 0)) / 60)),
            mode="实时驾车",
            polyline=[
                coordinate
                for step in path.get("steps", [])
                for coordinate in _polyline(step.get("polyline", ""))
            ],
            source=DataSource(provider="amap", freshness="realtime", fetched_at=_now()),
        )


class OpenMeteoClient:
    """无需 API Key 的天气预报客户端，负责把 WMO 代码转成中文事实。"""

    CACHE_TTL_SECONDS = 30 * 60

    def __init__(self, settings: Settings, cache_store: CacheStore | None = None) -> None:
        self.settings = settings
        self.cache_store = cache_store
        self.request_lock = Lock()
        self.http = httpx.Client(timeout=settings.open_meteo_timeout_seconds)

    def get_weather(self, city: City, start_date: date, end_date: date) -> list[WeatherDay]:
        """查询每日最高最低温和天气代码，日期不覆盖时返回已有结果。"""

        if city.latitude is None or city.longitude is None:
            raise ProviderError("weather_unavailable", "目的地缺少天气查询坐标")
        key = stable_key(
            round(city.latitude, 5),
            round(city.longitude, 5),
            start_date,
            end_date,
        )
        payload = self._cached_payload(key)
        if payload is None:
            payload = self._fetch_once(key, city, start_date, end_date)
        return _open_meteo_weather(payload)

    def _fetch_once(
        self,
        key: str,
        city: City,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        """合并重复请求；缓存只是优化，损坏或缺失时重新查询。"""

        with self.request_lock:
            cached = self._cached_payload(key)
            if cached is not None:
                return cached
            payload = {
                **self._request(city, start_date, end_date),
                "_openzl_fetched_at": _now().isoformat(),
            }
            if self.cache_store is not None:
                self.cache_store.set_cache(
                    "open_meteo", key, payload, self.CACHE_TTL_SECONDS
                )
            return payload

    def _cached_payload(self, key: str) -> dict[str, Any] | None:
        if self.cache_store is None:
            return None
        value = self.cache_store.get_cache("open_meteo", key)
        return cast(dict[str, Any], value) if isinstance(value, dict) else None

    def _request(
        self, city: City, start_date: date, end_date: date
    ) -> dict[str, Any]:
        try:
            response = self.http.get(
                self.settings.open_meteo_base_url,
                params={
                    "latitude": city.latitude,
                    "longitude": city.longitude,
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Shanghai",
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("weather_unavailable", "Open-Meteo 天气服务暂时不可用") from exc
        if not isinstance(payload, dict):
            raise ProviderError("weather_unavailable", "Open-Meteo 返回了无法识别的数据")
        return cast(dict[str, Any], payload)


def _open_meteo_weather(payload: dict[str, Any]) -> list[WeatherDay]:
    """把缓存或网络响应统一转换为领域天气事实。"""

    daily = payload.get("daily", {})
    if not isinstance(daily, dict):
        return []
    fetched_at = _source_time(payload.get("_openzl_fetched_at"))
    dates = daily.get("time", [])
    codes = daily.get("weather_code", [])
    highs = daily.get("temperature_2m_max", [])
    lows = daily.get("temperature_2m_min", [])
    return [
        WeatherDay(
            date=date.fromisoformat(item_date),
            day_weather=_wmo_text(code),
            night_weather=_wmo_text(code),
            day_temperature=_temperature(high),
            night_temperature=_temperature(low),
            source=DataSource(
                provider="open_meteo",
                freshness="forecast",
                fetched_at=fetched_at,
            ),
        )
        for item_date, code, high, low in zip(dates, codes, highs, lows, strict=False)
    ]


def _source_time(value: Any) -> datetime:
    """恢复缓存中的首次抓取时间，旧缓存缺失时使用当前时间。"""

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return _now()


def _parse_poi(raw: dict[str, Any], category: str) -> Poi | None:
    latitude, longitude = _location(raw.get("location"), from_amap=True)
    if latitude is None or longitude is None or not raw.get("id") or not raw.get("name"):
        return None
    return Poi(
        id=raw["id"],
        name=raw["name"],
        address=_text(raw.get("address")),
        category=category,  # type: ignore[arg-type]
        latitude=latitude,
        longitude=longitude,
        type_name=_text(raw.get("type")),
        image_url=_photo_url(raw.get("photos")),
    )


def _location(value: Any, from_amap: bool = False) -> tuple[float | None, float | None]:
    if not isinstance(value, str) or "," not in value:
        return None, None
    longitude_text, latitude_text = value.split(",", 1)
    try:
        latitude, longitude = float(latitude_text), float(longitude_text)
        return _gcj02_to_wgs84(latitude, longitude) if from_amap else (latitude, longitude)
    except ValueError:
        return None, None


def _amap_location(poi: Poi) -> str:
    """调用高德前把领域层 WGS-84 坐标转换为高德使用的 GCJ-02。"""

    latitude, longitude = _wgs84_to_gcj02(poi.latitude, poi.longitude)
    return f"{longitude},{latitude}"


def _polyline(value: str) -> list[Coordinate]:
    points: list[Coordinate] = []
    for item in value.split(";"):
        latitude, longitude = _location(item, from_amap=True)
        if latitude is not None and longitude is not None:
            points.append(Coordinate(latitude=latitude, longitude=longitude))
    return points


def _local_routes(day_pois: Sequence[Poi], mode: str) -> list[RouteSegment]:
    """为普通步行/驾车提供低成本估算，明确不生成虚假轨迹。"""

    return [
        _local_route(left, right, _choose_auto_mode(left, right, mode))
        for left, right in zip(day_pois, day_pois[1:], strict=False)
    ]


def local_routes(day_pois: Sequence[Poi], mode: str) -> list[RouteSegment]:
    """公开本地交通估算，供工作流在测试替身或离线模式下使用。"""

    return _local_routes(day_pois, mode)


def _choose_auto_mode(from_poi: Poi, to_poi: Poi, mode: str) -> str:
    if mode != "auto":
        return mode
    return "walk" if _haversine(from_poi, to_poi) <= 3 else "driving"


def _local_route(
    from_poi: Poi,
    to_poi: Poi,
    mode: str,
    via_pois: Sequence[Poi] = (),
) -> RouteSegment:
    points = [from_poi, *via_pois, to_poi]
    distance = sum(
        _haversine(left, right) for left, right in zip(points, points[1:], strict=False)
    )
    distance *= 1.2 if mode == "walk" else 1.4
    speed = 4.5 if mode == "walk" else 25.0
    label = "步行估算" if mode == "walk" else "驾车估算"
    duration = max(1, round(distance / speed * 60))
    return RouteSegment(
        from_poi_id=from_poi.id,
        to_poi_id=to_poi.id,
        via_poi_ids=[poi.id for poi in via_pois],
        distance_km=round(distance, 2),
        duration_minutes=duration,
        mode=label,
        source=DataSource(provider="local_estimate", freshness="estimated"),
    )


def _haversine(left: Poi, right: Poi) -> float:
    radius = 6371.0
    lat1, lat2 = math.radians(left.latitude), math.radians(right.latitude)
    delta_lat = math.radians(right.latitude - left.latitude)
    delta_lon = math.radians(right.longitude - left.longitude)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return radius * 2 * math.asin(math.sqrt(value))


def _covers_dates(weather: list[WeatherDay], start_date: date, end_date: date) -> bool:
    dates = {item.date for item in weather}
    return all(
        start_date <= item <= end_date and item in dates
        for item in _date_range(start_date, end_date)
    )


def _date_range(start_date: date, end_date: date) -> list[date]:
    return [
        start_date.fromordinal(value)
        for value in range(start_date.toordinal(), end_date.toordinal() + 1)
    ]


def _wmo_text(code: Any) -> str:
    try:
        number = int(code)
    except (TypeError, ValueError):
        return "天气未知"
    ranges = (
        ((0, 0), "晴"),
        ((1, 3), "多云"),
        ((45, 48), "雾"),
        ((51, 67), "小雨或冻雨"),
        ((71, 77), "降雪"),
        ((80, 82), "阵雨"),
        ((95, 99), "雷雨"),
    )
    return next((label for (lower, upper), label in ranges if lower <= number <= upper), "天气未知")


def _temperature(value: Any) -> str | None:
    return str(round(float(value))) if isinstance(value, (int, float)) else None


def _transit_lines(transit: dict[str, Any]) -> list[TransitLine]:
    lines: list[TransitLine] = []
    for segment in transit.get("segments", []):
        if not isinstance(segment, dict):
            continue
        for busline in segment.get("bus", {}).get("buslines", []):
            if not isinstance(busline, dict):
                continue
            lines.append(
                TransitLine(
                    name=_text(busline.get("name")) or "未知线路",
                    type=_text(busline.get("type")) or "公交",
                    departure_stop=_text(busline.get("departure_stop", {}).get("name")),
                    arrival_stop=_text(busline.get("arrival_stop", {}).get("name")),
                    via_stops=[
                        _text(stop.get("name"))
                        for stop in busline.get("via_stops", [])
                        if isinstance(stop, dict) and _text(stop.get("name"))
                    ],
                )
            )
    return lines


def _transit_polyline(transit: dict[str, Any]) -> list[Coordinate]:
    values: list[Coordinate] = []
    for segment in transit.get("segments", []):
        if not isinstance(segment, dict):
            continue
        walk = segment.get("walking", {})
        values.extend(_polyline(_text(walk.get("polyline"))))
        for busline in segment.get("bus", {}).get("buslines", []):
            if isinstance(busline, dict):
                values.extend(_polyline(_text(busline.get("polyline"))))
    return values


def _transport_key(kind: str, *values: str) -> str:
    return json.dumps([kind, *values], ensure_ascii=False)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _call_route(upstream: MapProvider, method: str, *args: Any) -> RouteSegment:
    operation = getattr(upstream, method, None)
    if operation is None:
        raise ProviderError("route_not_supported", "当前地图服务不支持该交通方式")
    return cast(RouteSegment, operation(*args))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gcj02_to_wgs84(latitude: float, longitude: float) -> tuple[float, float]:
    """将高德返回的 GCJ-02 坐标近似还原到领域层统一使用的 WGS-84。"""

    if _outside_china(latitude, longitude):
        return latitude, longitude
    adjusted_lat, adjusted_lon = _wgs84_to_gcj02(latitude, longitude)
    return latitude * 2 - adjusted_lat, longitude * 2 - adjusted_lon


def _wgs84_to_gcj02(latitude: float, longitude: float) -> tuple[float, float]:
    if _outside_china(latitude, longitude):
        return latitude, longitude
    d_lat = _transform_lat(longitude - 105.0, latitude - 35.0)
    d_lon = _transform_lon(longitude - 105.0, latitude - 35.0)
    rad_lat = latitude / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - 0.00669342162296594323 * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = d_lat * 180.0 / ((6335552.717000426 * (magic * sqrt_magic)) * math.pi)
    d_lon = d_lon * 180.0 / (6378245.0 / sqrt_magic * math.cos(rad_lat) * math.pi)
    return latitude + d_lat, longitude + d_lon


def _outside_china(latitude: float, longitude: float) -> bool:
    return not (73.0 <= longitude <= 135.0 and 3.0 <= latitude <= 54.0)


def _transform_lat(x: float, y: float) -> float:
    value = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y
    value += 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    value += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    value += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    value += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return value


def _transform_lon(x: float, y: float) -> float:
    value = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
    value += 0.1 * math.sqrt(abs(x))
    value += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    value += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    value += (
        150.0 * math.sin(x / 12.0 * math.pi)
        + 300.0 * math.sin(x / 30.0 * math.pi)
    ) * 2.0 / 3.0
    return value


def _first(value: Any) -> dict[str, Any] | None:
    return value[0] if isinstance(value, list) and value else None


def _text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(str(item) for item in value)
    return str(value or "")


def _photo_url(value: Any) -> str | None:
    """只接受高德照片列表中的 HTTP(S) 地址，不下载第三方图片。"""

    if not isinstance(value, list):
        return None
    for photo in value:
        url = photo.get("url") if isinstance(photo, dict) else None
        parsed = urlsplit(url) if isinstance(url, str) else None
        if parsed and parsed.scheme in {"http", "https"} and parsed.netloc:
            return url
    return None


def _in_range(value: Any, start_date: date, end_date: date) -> bool:
    try:
        item_date = date.fromisoformat(str(value))
    except ValueError:
        return False
    return start_date <= item_date <= end_date
