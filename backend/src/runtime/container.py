"""应用组合根：把配置、模型网关和具体 Provider 装配成 TravelGraph 依赖。

只有本模块知道各个实现类的构造方式。领域模型、图节点和 Provider 解析器都不读取全局
容器，从而避免隐式依赖和循环导入；测试可以直接构造 ``TravelDependencies`` 注入图。
"""

from __future__ import annotations

from typing import Literal

from catalog import CatalogTool, PostgresCatalogRepository
from domain.models import (
    CandidateCatalog,
    City,
    DestinationCandidate,
    HotelOption,
    Poi,
)
from providers.amap import AmapClient
from providers.base import stable_fact_id
from providers.fakes import (
    FakeCatalogProvider,
    FakeHotelProvider,
    FakeRailProvider,
    FakeRouteProvider,
    FakeWeatherProvider,
)
from providers.hotels import HotelProvider as LiveHotelProvider
from providers.hotels import RollingGoHotelClient
from providers.mcp import McpHttpClient
from providers.rail import RailClient
from providers.rail import RailProvider as LiveRailProvider
from providers.rail_12306 import Public12306Client
from providers.routes import RouteProvider as LiveRouteProvider
from providers.weather import OpenMeteoClient
from providers.weather import WeatherProvider as LiveWeatherProvider
from runtime.config import Settings, get_settings
from runtime.contracts import StructuredModel, TravelDependencies
from runtime.model_gateway import build_model_bundle

_default_dependencies: TravelDependencies | None = None


def get_dependencies(settings: Settings | None = None) -> TravelDependencies:
    """构造图的唯一依赖容器；默认配置在进程内复用连接池。

    新手可以把这里理解为“装配线”：先读配置，再选择 Fake 或 Live Provider，最后把
    完整容器交给 ``build_travel_graph``。业务节点不应在运行过程中自行 new Provider。
    """

    global _default_dependencies
    if settings is not None:
        return _build_dependencies(settings)
    if _default_dependencies is None:
        _default_dependencies = _build_dependencies(get_settings())
    return _default_dependencies


def reset_dependencies() -> None:
    """清理默认容器引用，仅供测试重新装配环境变量。"""

    global _default_dependencies
    _default_dependencies = None


def _build_dependencies(settings: Settings) -> TravelDependencies:
    """根据运行模式组装一份自洽依赖，确保 Fake 与 Live 具有相同 Protocol。"""

    models = build_model_bundle(settings)
    if settings.provider_mode == "fake":
        return _fake_dependencies(models.requirement, models.planner, models.review)

    amap = _amap_client(settings)
    repository = PostgresCatalogRepository(settings.catalog_database_url or "")
    catalog = CatalogTool(
        repository,
        amap if settings.allow_amap_fallback else None,
    )
    open_meteo = OpenMeteoClient(
        base_url=settings.open_meteo_base_url,
        timeout_seconds=settings.open_meteo_timeout_seconds,
    )
    # 这里的 Live* 别名用于区分“具体适配器”和 runtime.contracts 中的 Protocol。
    # 容器负责选择实现，图节点只看到 Protocol，不需要知道 Provider 的构造细节。
    weather = LiveWeatherProvider(open_meteo, amap)
    if settings.rail_provider == "public":
        # 默认直接访问 12306 公共查询接口，不要求用户另起一个本地 MCP 服务。
        rail_client: RailClient = Public12306Client(
            timeout_seconds=settings.rail_mcp_timeout_seconds
        )
    else:
        # 需要接入自建或第三方铁路 MCP 时，只切换 RAIL_PROVIDER=mcp。
        rail_client = McpHttpClient(
            settings.rail_mcp_url,
            timeout_seconds=settings.rail_mcp_timeout_seconds,
            bearer_token=settings.rail_mcp_token or "",
        )
    rail = LiveRailProvider(
        rail_client,
        timeout_seconds=settings.rail_mcp_timeout_seconds,
    )
    rollinggo = RollingGoHotelClient(
        settings.rollinggo_mcp_url,
        settings.rollinggo_api_key,
        timeout_seconds=settings.rollinggo_timeout_seconds,
    )
    hotels = LiveHotelProvider(
        rollinggo,
        timeout_seconds=settings.rollinggo_timeout_seconds,
    )
    return TravelDependencies(
        catalog=catalog,
        rail=rail,
        hotels=hotels,
        weather=weather,
        routes=LiveRouteProvider(amap),
        requirement_model=models.requirement,
        planner_model=models.planner,
        review_model=models.review,
    )


def _amap_client(settings: Settings) -> AmapClient | None:
    if settings.amap_api_key is None:
        return None
    return AmapClient(
        settings.amap_api_key,
        base_url=settings.amap_base_url,
        timeout_seconds=settings.amap_timeout_seconds,
    )


def _fake_dependencies(
    requirement_model: StructuredModel | None,
    planner_model: StructuredModel | None,
    review_model: StructuredModel | None,
) -> TravelDependencies:
    """构造不需要网络、数据库或模型密钥的完整离线依赖。"""

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
    return TravelDependencies(
        catalog=FakeCatalogProvider(city, catalog, destinations),
        rail=FakeRailProvider(),
        hotels=FakeHotelProvider(_fake_hotels(catalog)),
        weather=FakeWeatherProvider(),
        routes=FakeRouteProvider(),
        requirement_model=requirement_model,
        planner_model=planner_model,
        review_model=review_model,
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
