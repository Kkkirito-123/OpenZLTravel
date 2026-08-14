"""OpenZLTravel 的外部数据能力公共入口。

调用方从本模块导入稳定接口；具体地图、铁路、酒店和规划器实现分别保存在子模块中。
"""

from app.providers.base import CircuitBreaker, McpHttpClient, ProviderExecutor, stable_key
from app.providers.hotels import HotelProvider, RollingGoHotelClient
from app.providers.maps import (
    AmapClient,
    AmapScheduler,
    CatalogReader,
    HybridMapProvider,
    JsonResponseCache,
    MapProvider,
    OpenMeteoClient,
    TransportResult,
    WeatherProvider,
    _cache_key,
    _gcj02_to_wgs84,
    _is_rate_limited,
    _wgs84_to_gcj02,
    local_routes,
)
from app.providers.planner import CopyEnhancer, DeterministicPlanner, LlmPlanner, Planner
from app.providers.rail import RailProvider

__all__ = [
    "AmapClient",
    "AmapScheduler",
    "CatalogReader",
    "CircuitBreaker",
    "CopyEnhancer",
    "DeterministicPlanner",
    "HotelProvider",
    "HybridMapProvider",
    "JsonResponseCache",
    "LlmPlanner",
    "MapProvider",
    "McpHttpClient",
    "OpenMeteoClient",
    "Planner",
    "ProviderExecutor",
    "RailProvider",
    "RollingGoHotelClient",
    "TransportResult",
    "WeatherProvider",
    "_cache_key",
    "_gcj02_to_wgs84",
    "_is_rate_limited",
    "_wgs84_to_gcj02",
    "local_routes",
    "stable_key",
]
