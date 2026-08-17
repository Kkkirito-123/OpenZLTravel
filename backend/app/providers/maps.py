"""地图与交通的混合策略层。

本文件决定本地目录、高德兜底和交通降级的顺序，不解析高德 JSON，也不实现坐标公式。
``amap``、``weather``、``geo`` 分别承担外部协议、天气和纯地理计算；本模块保留历史导入
入口，避免调用方耦合到内部文件布局。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, cast

import httpx

from app.coordination import Coordination
from app.errors import ProviderError
from app.models import CandidateCatalog, City, Poi, RouteSegment, WeatherDay
from app.providers.amap import (
    AmapClient,
    JsonResponseCache,
    StoreResponseCache,
    cache_key_for,
    is_rate_limited,
)
from app.providers.geo import (
    gcj02_to_wgs84,
    local_route,
    local_routes,
    wgs84_to_gcj02,
)
from app.providers.weather import OpenMeteoClient, covers_dates


class MapProvider(Protocol):
    """旅行服务依赖的地图与天气能力。"""

    def resolve_city(self, destination: str) -> City:
        """确认目的地城市。"""

    def search_candidates(self, city: City) -> CandidateCatalog:
        """返回城市内可供模型选择的真实 POI。"""

    def get_weather(
        self,
        city: City,
        start_date: date,
        end_date: date,
    ) -> list[WeatherDay]:
        """返回日期范围内供应商实际提供的天气。"""

    def get_route(self, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """返回两个真实 POI 之间的路线。"""


class WeatherProvider(Protocol):
    """天气供应商的最小同步接口，便于主来源和兜底来源替换。"""

    def get_weather(self, city: City, start_date: date, end_date: date) -> list[WeatherDay]:
        """查询指定日期范围的天气。"""


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

    def resolve_city(self, destination: str) -> City:
        """根据城市名返回本地城市事实。"""

    def search_candidates(self, city: City) -> CandidateCatalog:
        """根据城市坐标返回本地 POI 候选。"""


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

        local = self._local_city(destination)
        if local is not None:
            return local
        return self.upstream.resolve_city(destination)

    async def resolve_city_async(self, destination: str) -> City:
        """异步优先读取本地目录，只有兜底查询才进入高德调度器。"""

        local = await asyncio.to_thread(self._local_city, destination)
        if local is not None:
            return local
        result = await self._scheduled(
            _transport_key("city", destination),
            lambda: self.upstream.resolve_city(destination),
        )
        return cast(City, result)

    def search_candidates(self, city: City) -> CandidateCatalog:
        """优先使用 OSM POI，避免重复调用高德地点搜索接口。"""

        local = self._local_candidates(city)
        if local is not None:
            return local
        return self.upstream.search_candidates(city)

    async def search_candidates_async(self, city: City) -> CandidateCatalog:
        """通过调度器执行 POI 兜底查询；本地目录命中时不会产生高德请求。"""

        local = await asyncio.to_thread(self._local_candidates, city)
        if local is not None:
            return local
        result = await self._scheduled(
            _transport_key("candidates", city.name),
            lambda: self.upstream.search_candidates(city),
        )
        return cast(CandidateCatalog, result)

    def get_weather(self, city: City, start_date: date, end_date: date) -> list[WeatherDay]:
        """优先查询 Open-Meteo，失败或日期不完整时再使用高德兜底。"""

        if self.weather_provider is not None:
            try:
                weather = self.weather_provider.get_weather(city, start_date, end_date)
                if covers_dates(weather, start_date, end_date):
                    return weather
            except (ProviderError, httpx.HTTPError, ValueError):
                pass
        return self.upstream.get_weather(city, start_date, end_date)

    async def get_weather_async(
        self, city: City, start_date: date, end_date: date
    ) -> list[WeatherDay]:
        """异步天气查询；Open-Meteo 成功时完全跳过高德天气接口。"""

        if self.weather_provider is not None:
            try:
                weather = await asyncio.to_thread(
                    self.weather_provider.get_weather, city, start_date, end_date
                )
                if covers_dates(weather, start_date, end_date):
                    return weather
            except (ProviderError, httpx.HTTPError, ValueError):
                pass
        result = await self._scheduled(
            _transport_key("weather", city.name, start_date.isoformat(), end_date.isoformat()),
            lambda: self.upstream.get_weather(city, start_date, end_date),
        )
        return cast(list[WeatherDay], result)

    def get_route(self, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """路线仍使用高德驾车轨迹，OSM POI 不直接等同于驾车路线。"""

        return self.upstream.get_route(from_poi, to_poi)

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
            return TransportResult(local_routes(day_pois, mode), [])
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
                routes.append(local_route(left, right, "walk"))
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
                [local_route(day_pois[0], day_pois[-1], "driving", day_pois[1:-1])],
                ["实时驾车路线暂时不可用，当前显示为本地驾车估算。"],
            )

    async def _scheduled(self, key: str, operation: Callable[[], Any]) -> Any:
        return await self.scheduler.run(key, lambda: asyncio.to_thread(operation))

    def _local_city(self, destination: str) -> City | None:
        """尝试本地城市索引；允许高德兜底时把未命中转为 ``None``。"""

        if not self.catalog.available:
            return None
        try:
            return self.catalog.resolve_city(destination)
        except LookupError as error:
            self._raise_without_fallback(error)
            return None

    def _local_candidates(self, city: City) -> CandidateCatalog | None:
        """尝试本地 POI 候选；不把本地未覆盖误当作高德调用。"""

        if not self.catalog.available:
            return None
        try:
            return self.catalog.search_candidates(city)
        except LookupError as error:
            self._raise_without_fallback(error)
            return None

    def _raise_without_fallback(self, error: LookupError) -> None:
        if not self.allow_amap_fallback:
            raise ProviderError("local_data_not_found", str(error)) from error


class AmapScheduler:
    """统一限制高德异步调用，并合并同一进程内的重复请求。"""

    def __init__(
        self,
        concurrency: int = 2,
        min_interval_seconds: float = 0.4,
        coordination: Coordination | None = None,
    ) -> None:
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.min_interval_seconds = min_interval_seconds
        self.last_request_at = 0.0
        self.slot_lock = asyncio.Lock()
        self.inflight_lock = asyncio.Lock()
        self.inflight: dict[str, asyncio.Task[Any]] = {}
        self.coordination = coordination

    async def run(self, key: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        """相同 key 共享任务，失败直接返回，不做危险的立即重试。"""

        async with self.inflight_lock:
            task = self.inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._execute(key, operation))
                self.inflight[key] = task
                task.add_done_callback(lambda completed: self._discard_inflight(key, completed))
        return await asyncio.shield(task)

    def _discard_inflight(self, key: str, completed: asyncio.Future[Any]) -> None:
        """由完成任务按身份清理，防止一个取消的等待者破坏请求合并。"""

        if self.inflight.get(key) is completed:
            self.inflight.pop(key, None)
        if not completed.cancelled():
            with contextlib.suppress(Exception):
                completed.exception()

    async def _execute(self, key: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        async with _amap_request_lock(self.coordination, key):
            async with self.semaphore:
                async with _amap_slot(self.coordination):
                    async with self.slot_lock:
                        elapsed = time.monotonic() - self.last_request_at
                        wait = self.min_interval_seconds - elapsed
                        if wait > 0:
                            await asyncio.sleep(wait)
                        self.last_request_at = time.monotonic()
                    return await operation()


@contextlib.asynccontextmanager
async def _amap_slot(coordination: Coordination | None) -> Any:
    if coordination is None:
        yield
        return
    async with coordination.provider_slot("amap"):
        yield


@contextlib.asynccontextmanager
async def _amap_request_lock(coordination: Coordination | None, key: str) -> Any:
    if coordination is None:
        yield
        return
    async with coordination.request_lock("amap", key):
        yield


def _transport_key(kind: str, *values: str) -> str:
    return json.dumps([kind, *values], ensure_ascii=False)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _call_route(upstream: MapProvider, method: str, *args: Any) -> RouteSegment:
    operation = getattr(upstream, method, None)
    if operation is None:
        raise ProviderError("route_not_supported", "当前地图服务不支持该交通方式")
    return cast(RouteSegment, operation(*args))


# 历史导入兼容：外部模块仍可从 app.providers.maps / app.providers 导入这些符号。
_cache_key = cache_key_for
_is_rate_limited = is_rate_limited
_gcj02_to_wgs84 = gcj02_to_wgs84
_wgs84_to_gcj02 = wgs84_to_gcj02

__all__ = [
    "AmapClient",
    "AmapScheduler",
    "CatalogReader",
    "HybridMapProvider",
    "JsonResponseCache",
    "MapProvider",
    "OpenMeteoClient",
    "StoreResponseCache",
    "TransportResult",
    "WeatherProvider",
    "_cache_key",
    "_gcj02_to_wgs84",
    "_is_rate_limited",
    "_wgs84_to_gcj02",
    "local_routes",
]
