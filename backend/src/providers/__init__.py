"""旅行工作台的只读外部事实 Provider。"""

from __future__ import annotations

from .amap import AmapClient
from .base import (
    AsyncTTLCache,
    CatalogUnavailableError,
    ProviderError,
    ProviderRuntime,
    stable_fact_id,
    stable_key,
)
from .fakes import (
    FakeCatalogProvider,
    FakeHotelProvider,
    FakeRailProvider,
    FakeRouteProvider,
    FakeWeatherProvider,
)
from .hotels import HotelProvider, RollingGoHotelClient
from .mcp import McpHttpClient
from .rail import RailProvider
from .rail_12306 import Public12306Client
from .routes import RouteProvider
from .weather import OpenMeteoClient, WeatherProvider

__all__ = [
    "AmapClient",
    "AsyncTTLCache",
    "CatalogUnavailableError",
    "FakeCatalogProvider",
    "FakeHotelProvider",
    "FakeRailProvider",
    "FakeRouteProvider",
    "FakeWeatherProvider",
    "HotelProvider",
    "McpHttpClient",
    "OpenMeteoClient",
    "ProviderError",
    "ProviderRuntime",
    "Public12306Client",
    "RailProvider",
    "RollingGoHotelClient",
    "RouteProvider",
    "WeatherProvider",
    "stable_fact_id",
    "stable_key",
]
