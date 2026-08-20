"""地图、天气、铁路、酒店和路线 Provider 的离线测试。"""

import json
from datetime import date
from typing import Any

import httpx
import pytest

from domain.models import CandidateCatalog, City, Poi, TravelRequirements
from providers.amap import AmapClient
from providers.base import ProviderError
from providers.hotels import HotelProvider
from providers.mcp import McpHttpClient
from providers.rail import RailProvider
from providers.rail_12306 import Public12306Client
from providers.routes import RouteProvider
from providers.weather import OpenMeteoClient, WeatherProvider
from runtime.config import get_settings
from runtime.container import get_dependencies, reset_dependencies


def _poi(identifier: str, longitude: float = 120.1) -> Poi:
    return Poi(
        id=identifier,
        name=identifier,
        category="attraction",
        latitude=30.1,
        longitude=longitude,
    )


def test_default_dependency_factory_uses_offline_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("PROVIDER_MODE", "fake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    reset_dependencies()

    dependencies = get_dependencies()

    assert dependencies.catalog.__class__.__name__ == "FakeCatalogProvider"
    assert dependencies.rail.__class__.__name__ == "FakeRailProvider"
    reset_dependencies()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_mcp_client_initializes_session_before_tool_call() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        methods.append(method)
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-06-18"},
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert request.headers["Mcp-Session-Id"] == "session-1"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"structuredContent": {"value": "ok"}},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = McpHttpClient("https://mcp.invalid", timeout_seconds=1, http=http)

    result = await client.call_tool("demo", {"query": "test"})

    assert result == {"value": "ok"}
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    await http.aclose()


@pytest.mark.asyncio
async def test_amap_keeps_stable_ids_and_only_real_polyline() -> None:
    city_parameters: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/place/text"):
            city_parameters.append(request.url.params["city"])
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "pois": [
                        {
                            "id": "raw-1",
                            "name": "西湖",
                            "location": "120.15,30.25",
                            "address": "杭州",
                            "type": "风景名胜;湖泊",
                        }
                    ],
                },
            )
        if request.url.path.endswith("/weather/weatherInfo"):
            city_parameters.append(request.url.params["city"])
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "forecasts": [
                        {
                            "casts": [
                                {
                                    "date": "2026-08-20",
                                    "dayweather": "晴",
                                    "nightweather": "晴",
                                }
                            ]
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "1",
                "route": {
                    "paths": [
                        {
                            "distance": "1200",
                            "duration": "600",
                            "steps": [{"polyline": "120.1,30.1;120.2,30.2"}],
                        }
                    ]
                },
            },
        )

    http = httpx.AsyncClient(
        base_url="https://amap.invalid/v3", transport=httpx.MockTransport(handler)
    )
    client = AmapClient("test-key", http=http)
    city = City(name="杭州", adcode="330100000000", latitude=30.2, longitude=120.1)

    catalog = await client.search_candidates(city)
    weather = await client.get_weather(city, date(2026, 8, 20), date(2026, 8, 20))
    route = await client.get_route(_poi("a"), _poi("b", 120.2))

    assert catalog.attractions[0].id.startswith("poi-amap:")
    assert catalog.attractions[0].tags == ["风景名胜", "湖泊"]
    assert weather[0].day_weather == "晴"
    assert city_parameters == ["330100", "330100", "330100", "330100"]
    assert route.source == "amap"
    assert len(route.polyline) == 2
    await http.aclose()


@pytest.mark.asyncio
async def test_amap_enrichment_prioritizes_hotel_display_fields() -> None:
    keywords: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keyword = request.url.params["keywords"]
        keywords.append(keyword)
        return httpx.Response(
            200,
            json={
                "status": "1",
                "pois": [
                    {
                        "id": f"amap-{keyword}",
                        "name": keyword,
                        "location": "120.155,30.274",
                        "address": f"{keyword}真实地址",
                        "type": "住宿服务;星级酒店",
                        "photos": [{"url": "https://example.com/hotel.jpg"}],
                    }
                ],
            },
        )

    http = httpx.AsyncClient(
        base_url="https://amap.invalid/v3", transport=httpx.MockTransport(handler)
    )
    client = AmapClient("test-key", http=http)
    city = City(name="杭州", adcode="330100000000", latitude=30.2, longitude=120.1)
    catalog = CandidateCatalog(
        attractions=[_poi("西湖")],
        hotels=[
            Poi(
                id="hotel-1",
                name="湖畔酒店",
                category="hotel",
                latitude=30.2,
                longitude=120.1,
            ),
            Poi(
                id="hotel-2",
                name="西湖宾馆",
                category="hotel",
                latitude=30.2,
                longitude=120.1,
            ),
        ],
    )

    enriched = await client.enrich_catalog(city, catalog, limit=2)

    assert keywords == ["湖畔酒店", "西湖宾馆"]
    assert enriched.hotels[0].address == "湖畔酒店真实地址"
    assert enriched.hotels[0].image_url == "https://example.com/hotel.jpg"
    assert enriched.attractions[0].address == ""
    await http.aclose()


@pytest.mark.asyncio
async def test_weather_falls_back_to_explicit_unknown_days() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    primary = OpenMeteoClient(http=http)
    provider = WeatherProvider(primary)
    city = City(name="杭州", latitude=30.2, longitude=120.1)

    weather = await provider.get_weather(city, date(2026, 8, 20), date(2026, 8, 21))

    assert len(weather) == 2
    assert all(item.day_weather is None for item in weather)
    assert all(item.warning == "暂无可靠天气预报" for item in weather)
    await http.aclose()


class FakeRailClient:
    """返回可解析的 12306 MCP 结果。"""

    def __init__(self) -> None:
        self.ticket_arguments: list[dict[str, Any]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "search-stations":
            return {"stations": [{"code": "HZH"}]}
        if name == "query-tickets":
            self.ticket_arguments.append(arguments)
            return {
                "trains": [
                    {
                        "train_code": "G1",
                        "from_station": "HZH",
                        "to_station": "SHH",
                        "start_time": "09:00",
                        "arrive_time": "10:00",
                        "duration": "01:00",
                        "seats": {"second_class": "有"},
                    }
                ]
            }
        return {"data": [{"train_code": "G1", "prices": {"二等座": "73"}}]}


@pytest.mark.asyncio
async def test_rail_combines_ticket_and_real_price() -> None:
    client = FakeRailClient()
    provider = RailProvider(client)

    options, cache_hit = await provider.search(
        "杭州", "SHH", date(2026, 8, 20), "outbound"
    )

    assert cache_hit is False
    assert options[0].price_from == 73
    assert options[0].has_ticket is True
    assert options[0].option_id.startswith("rail:")

    await provider.search("杭州", "SHH", date(2026, 8, 21), "return")
    assert client.ticket_arguments[-1]["from_station"] == "SHH"
    assert client.ticket_arguments[-1]["to_station"] == "HZH"


@pytest.mark.asyncio
async def test_public_12306_client_uses_zlagent_query_flow() -> None:
    """验证 ZLAgent 同款的公共 12306 查询链路可被当前 RailProvider 复用。"""

    fields = [""] * 36
    fields[1] = "Y"
    fields[2] = "TRAIN_NO"
    fields[3] = "G123"
    fields[6] = "HZH"
    fields[7] = "SHH"
    fields[8] = "09:00"
    fields[9] = "11:00"
    fields[10] = "02:00"
    fields[11] = "Y"
    fields[16] = "01"
    fields[17] = "02"
    fields[26] = "无"
    fields[28] = "无"
    fields[29] = "有"
    fields[30] = "有"
    fields[31] = "--"
    fields[32] = "无"
    fields[35] = "OMO"
    raw_row = "|".join(fields)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("station_name.js"):
            return httpx.Response(
                200,
                text="var station_names ='@a|杭州东|HZH|hangzhoudong@b|上海|SHH|shanghai';",
            )
        if request.url.path.endswith("/leftTicket/init"):
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/leftTicket/queryG"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "result": [raw_row],
                        "map": {"HZH": "杭州东", "SHH": "上海"},
                    }
                },
            )
        if request.url.path.endswith("queryTicketPrice"):
            return httpx.Response(200, json={"data": {"O": "73.00"}})
        return httpx.Response(200, json={"data": {"result": []}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = Public12306Client(http=http)
    provider = RailProvider(client)

    options, cache_hit = await provider.search(
        "杭州", "SHH", date(2026, 8, 20), "outbound"
    )

    assert cache_hit is False
    assert options[0].train_code == "G123"
    assert options[0].from_station == "杭州东"
    assert options[0].price_from == 73
    await http.aclose()


@pytest.mark.asyncio
async def test_rollinggo_client_uses_search_hotels_tool_without_token_file() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeRollingGo:
        async def search(self, arguments: dict[str, Any]) -> Any:
            calls.append(("searchHotels", arguments))
            return {"hotels": []}

    from providers.hotels import RollingGoHotelClient

    client = RollingGoHotelClient("https://mcp.invalid/mcp", "", client=FakeRollingGo())
    result = await client.search({"place": "杭州"})

    assert result == {"hotels": []}
    assert calls == [("searchHotels", {"place": "杭州"})]


class FailedHotelClient:
    """模拟 RollingGo 不可用。"""

    async def search(self, arguments: dict[str, Any]) -> Any:
        raise ProviderError("rollinggo_unavailable", "offline")


@pytest.mark.asyncio
async def test_hotel_failure_uses_catalog_without_guessing_price() -> None:
    hotel_poi = Poi(
        id="poi:catalog:hotel",
        name="湖畔酒店",
        category="hotel",
        latitude=30.2,
        longitude=120.1,
    )
    provider = HotelProvider(FailedHotelClient())
    requirements = TravelRequirements(
        origin="上海",
        destination="杭州",
        start_date=date(2026, 8, 20),
        end_date=date(2026, 8, 22),
    )

    options, cache_hit, warning = await provider.search(
        requirements, CandidateCatalog(hotels=[hotel_poi])
    )

    assert cache_hit is False
    assert warning is not None
    assert options[0].source == "osm"
    assert options[0].price_per_night is None


class FailedRoutes:
    """模拟高德实时路线失败。"""

    async def get_route_with_waypoints(self, day_pois: list[Poi]) -> Any:
        raise ProviderError("route_failed", "offline")

    async def get_transit(self, city: City, from_poi: Poi, to_poi: Poi) -> Any:
        raise ProviderError("route_failed", "offline")


@pytest.mark.asyncio
async def test_route_failure_uses_estimate_without_fake_polyline() -> None:
    provider = RouteProvider(FailedRoutes())

    routes, warnings = await provider.get_routes(
        City(name="杭州"), [_poi("a"), _poi("b", 120.2)], "realtime_driving"
    )

    assert warnings
    assert routes[0].source == "local_estimate"
    assert routes[0].polyline == []
