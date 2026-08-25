"""Assistant 与 TravelGraph 的最小依赖装配根。"""

from __future__ import annotations

from typing import Literal

from openzltravel.domain.models import (
    CandidateCatalog,
    City,
    DestinationCandidate,
    HotelOption,
    Poi,
)
from openzltravel.infrastructure.catalog.tool import CatalogTool, PostgresCatalogRepository
from openzltravel.infrastructure.providers.amap import AmapClient
from openzltravel.infrastructure.providers.base import stable_fact_id
from openzltravel.infrastructure.providers.fakes import (
    FakeCatalogProvider,
    FakeHotelProvider,
    FakeRailProvider,
    FakeRouteProvider,
    FakeWeatherProvider,
)
from openzltravel.infrastructure.providers.hotels import HotelProvider as LiveHotelProvider
from openzltravel.infrastructure.providers.hotels import RollingGoHotelClient
from openzltravel.infrastructure.providers.mcp import McpHttpClient
from openzltravel.infrastructure.providers.rail import RailClient
from openzltravel.infrastructure.providers.rail import RailProvider as LiveRailProvider
from openzltravel.infrastructure.providers.rail_12306 import Public12306Client
from openzltravel.infrastructure.providers.routes import RouteProvider as LiveRouteProvider
from openzltravel.infrastructure.providers.weather import OpenMeteoClient
from openzltravel.infrastructure.providers.weather import WeatherProvider as LiveWeatherProvider
from openzltravel.runtime.config import ConfigurationError, Settings, get_settings
from openzltravel.runtime.contracts import AssistantDependencies, PlanningDependencies

_assistant_dependencies: AssistantDependencies | None = None
_planning_dependencies: PlanningDependencies | None = None


def get_assistant_dependencies(settings: Settings | None = None) -> AssistantDependencies:
    """装配交流助手的目录、铁路、酒店和天气只读工具。"""

    global _assistant_dependencies
    if settings is not None:
        return _build_assistant_dependencies(settings)
    if _assistant_dependencies is None:
        _assistant_dependencies = _build_assistant_dependencies(get_settings())
    return _assistant_dependencies


def get_planning_dependencies(settings: Settings | None = None) -> PlanningDependencies:
    """装配 TravelGraph 唯一需要的路线 Provider。"""

    global _planning_dependencies
    if settings is not None:
        return _build_planning_dependencies(settings)
    if _planning_dependencies is None:
        _planning_dependencies = _build_planning_dependencies(get_settings())
    return _planning_dependencies


def reset_dependencies() -> None:
    """清理进程级容器引用，仅供测试重新装配配置。"""

    global _assistant_dependencies, _planning_dependencies
    _assistant_dependencies = None
    _planning_dependencies = None


def _build_assistant_dependencies(settings: Settings) -> AssistantDependencies:
    if settings.provider_mode == "fake":
        city = City(name="杭州", adcode="330100", latitude=30.2741, longitude=120.1551)
        catalog = _fake_catalog()
        destinations = [
            DestinationCandidate(
                candidate_id=stable_fact_id("destination", "330100"),
                city=city,
                score=0.95,
                reasons=["景点、餐饮和住宿覆盖充足", "离线开发数据"],
                attraction_count=len(catalog.attractions),
                restaurant_count=len(catalog.restaurants),
                hotel_count=len(catalog.hotels),
            )
        ]
        return AssistantDependencies(
            catalog=FakeCatalogProvider(city, catalog, destinations),
            rail=FakeRailProvider(),
            hotels=FakeHotelProvider(_fake_hotels(catalog)),
            weather=FakeWeatherProvider(),
        )

    if settings.catalog_database_url is None:
        raise ConfigurationError("Assistant 的 PROVIDER_MODE=live 要求 CATALOG_DATABASE_URL")
    amap = _amap_client(settings)
    repository = PostgresCatalogRepository(settings.catalog_database_url or "")
    catalog_tool = CatalogTool(repository, amap if settings.allow_amap_fallback else None)
    weather = LiveWeatherProvider(
        OpenMeteoClient(
            base_url=settings.open_meteo_base_url,
            timeout_seconds=settings.open_meteo_timeout_seconds,
        ),
        amap,
    )
    rail_client: RailClient
    if settings.rail_provider == "public":
        rail_client = Public12306Client(timeout_seconds=settings.rail_mcp_timeout_seconds)
    else:
        rail_client = McpHttpClient(
            settings.rail_mcp_url,
            timeout_seconds=settings.rail_mcp_timeout_seconds,
            bearer_token=settings.rail_mcp_token or "",
        )
    return AssistantDependencies(
        catalog=catalog_tool,
        rail=LiveRailProvider(rail_client, timeout_seconds=settings.rail_mcp_timeout_seconds),
        hotels=LiveHotelProvider(
            RollingGoHotelClient(
                settings.rollinggo_mcp_url,
                settings.rollinggo_api_key,
                timeout_seconds=settings.rollinggo_timeout_seconds,
            ),
            timeout_seconds=settings.rollinggo_timeout_seconds,
        ),
        weather=weather,
    )


def _build_planning_dependencies(settings: Settings) -> PlanningDependencies:
    if settings.provider_mode == "fake":
        return PlanningDependencies(routes=FakeRouteProvider())
    return PlanningDependencies(routes=LiveRouteProvider(_amap_client(settings)))


def _amap_client(settings: Settings) -> AmapClient | None:
    if settings.amap_api_key is None:
        return None
    return AmapClient(
        settings.amap_api_key,
        base_url=settings.amap_base_url,
        timeout_seconds=settings.amap_timeout_seconds,
    )


def _fake_catalog() -> CandidateCatalog:
    return CandidateCatalog(
        attractions=[
            _fake_poi("west-lake", "西湖", "attraction", 30.259, 120.139),
            _fake_poi("museum", "浙江省博物馆", "attraction", 30.253, 120.143),
            _fake_poi("lingyin", "灵隐寺", "attraction", 30.24, 120.102),
        ],
        restaurants=[
            _fake_poi("restaurant-1", "杭帮菜餐厅", "restaurant", 30.26, 120.15),
            _fake_poi("restaurant-2", "湖滨餐厅", "restaurant", 30.255, 120.16),
        ],
        hotels=[
            _fake_poi("hotel-1", "湖滨酒店", "hotel", 30.257, 120.158),
            _fake_poi("hotel-2", "西湖宾馆", "hotel", 30.25, 120.14),
        ],
    )


def _fake_poi(
    suffix: str,
    name: str,
    category: Literal["attraction", "restaurant", "hotel"],
    latitude: float,
    longitude: float,
) -> Poi:
    return Poi(
        id=f"poi:fake:{suffix}",
        name=name,
        category=category,
        latitude=latitude,
        longitude=longitude,
        tags=["离线开发数据"],
    )


def _fake_hotels(catalog: CandidateCatalog) -> list[HotelOption]:
    return [
        HotelOption(
            hotel_id=item.id,
            name=item.name,
            latitude=item.latitude,
            longitude=item.longitude,
            source="osm",
        )
        for item in catalog.hotels
    ]
