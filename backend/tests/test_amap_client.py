from datetime import date

import httpx
import pytest

from app.config import Settings
from app.errors import ProviderError
from app.models import City, Poi
from app.providers import AmapClient, _gcj02_to_wgs84
from tests.sqlite_repository import SqliteTripRepository


def test_amap_response_is_converted_to_domain_models() -> None:
    client = AmapClient(Settings(amap_api_key="test"))
    client._get = lambda path, **params: {
        "/geocode/geo": {
            "status": "1",
            "geocodes": [{"city": "测试市", "adcode": "123", "location": "120.1,30.1"}],
        },
        "/place/text": {
            "status": "1",
            "pois": [
                {
                    "id": "p1",
                    "name": "湖畔公园",
                    "address": "湖畔路 1 号",
                    "location": "120.2,30.2",
                    "type": "风景名胜",
                    "photos": [
                        {"url": "ftp://invalid.example.com/photo.jpg"},
                        {"url": "https://images.example.com/park.jpg"},
                    ],
                }
            ],
        },
        "/weather/weatherInfo": {
            "status": "1",
            "forecasts": [
                {
                    "casts": [
                        {
                            "date": "2026-09-01",
                            "dayweather": "晴",
                            "nightweather": "晴",
                            "daytemp": "25",
                            "nighttemp": "18",
                        }
                    ]
                }
            ],
        },
    }[path]

    city = client.resolve_city("测试市")
    catalog = client.search_candidates(city)
    weather = client.get_weather(city, date(2026, 9, 1), date(2026, 9, 1))

    city_latitude, city_longitude = _gcj02_to_wgs84(30.1, 120.1)
    assert city == City(
        name="测试市",
        adcode="123",
        latitude=city_latitude,
        longitude=city_longitude,
    )
    assert catalog.attractions[0].id == "p1"
    assert catalog.attractions[0].latitude == pytest.approx(_gcj02_to_wgs84(30.2, 120.2)[0])
    assert catalog.attractions[0].image_url == "https://images.example.com/park.jpg"
    assert weather[0].day_weather == "晴"


@pytest.mark.parametrize(
    "photos",
    [None, {}, [{"url": "file:///tmp/photo.jpg"}], [{"url": "https:///photo.jpg"}], ["bad"]],
)
def test_invalid_photo_data_is_ignored(photos: object) -> None:
    client = AmapClient(Settings(amap_api_key="test"))
    client._get = lambda path, **params: {
        "status": "1",
        "pois": [
            {
                "id": "p1",
                "name": "湖畔公园",
                "location": "120.2,30.2",
                "photos": photos,
            }
        ],
    }

    catalog = client.search_candidates(City(name="测试市"))

    assert catalog.attractions[0].image_url is None


def test_empty_weather_response_is_safe() -> None:
    client = AmapClient(Settings(amap_api_key="test"))
    client._get = lambda path, **params: {"status": "1", "forecasts": []}

    assert client.get_weather(City(name="测试市"), date(2026, 9, 1), date(2026, 9, 1)) == []


def test_malformed_weather_items_are_ignored() -> None:
    client = AmapClient(Settings(amap_api_key="test"))
    client._get = lambda path, **params: {
        "status": "1",
        "forecasts": [
            {
                "casts": [
                    "not-an-object",
                    {"date": "invalid"},
                    {"date": "2026-09-01", "dayweather": "晴", "nightweather": "多云"},
                ]
            }
        ],
    }

    weather = client.get_weather(City(name="测试市"), date(2026, 9, 1), date(2026, 9, 1))

    assert [item.date for item in weather] == [date(2026, 9, 1)]


def test_route_parser_keeps_real_coordinates() -> None:
    client = AmapClient(Settings(amap_api_key="test"))
    client._get = lambda path, **params: {
        "status": "1",
        "route": {
            "paths": [
                {
                    "distance": "2500",
                    "duration": "900",
                    "steps": [{"polyline": "120.1,30.1;120.2,30.2"}],
                }
            ]
        },
    }
    from_poi = Poi(id="p1", name="起点", category="attraction", latitude=30.1, longitude=120.1)
    to_poi = Poi(id="p2", name="终点", category="attraction", latitude=30.2, longitude=120.2)

    route = client.get_route(from_poi, to_poi)

    assert route.distance_km == 2.5
    assert route.duration_minutes == 15
    latitude, longitude = _gcj02_to_wgs84(30.1, 120.1)
    assert route.polyline[0].latitude == pytest.approx(latitude)
    assert route.polyline[0].longitude == pytest.approx(longitude)


def test_malformed_route_metrics_use_stable_error() -> None:
    client = AmapClient(Settings(amap_api_key="test"))
    client._get = lambda path, **params: {
        "status": "1",
        "route": {"paths": [{"distance": "unknown", "duration": None, "steps": [None]}]},
    }
    from_poi = Poi(id="p1", name="起点", category="attraction", latitude=30.1, longitude=120.1)
    to_poi = Poi(id="p2", name="终点", category="attraction", latitude=30.2, longitude=120.2)

    with pytest.raises(ProviderError) as error:
        client.get_route(from_poi, to_poi)

    assert error.value.code == "route_not_found"


def test_transit_parser_ignores_malformed_nested_fields() -> None:
    client = AmapClient(Settings(amap_api_key="test"))
    client._get = lambda path, **params: {
        "status": "1",
        "route": {
            "transits": [
                {
                    "distance": "1200",
                    "duration": "900",
                    "segments": [{"walking": "bad", "bus": {"buslines": [None, "bad"]}}],
                }
            ]
        },
    }
    from_poi = Poi(id="p1", name="起点", category="attraction", latitude=30.1, longitude=120.1)
    to_poi = Poi(id="p2", name="终点", category="attraction", latitude=30.2, longitude=120.2)

    route = client.get_transit(City(name="测试市"), from_poi, to_poi)

    assert route.distance_km == 1.2
    assert route.transit_lines == []
    assert route.polyline == []


def test_missing_route_path_uses_stable_error() -> None:
    client = AmapClient(Settings(amap_api_key="test"))
    client._get = lambda path, **params: {"status": "1", "route": {"paths": []}}
    from_poi = Poi(id="p1", name="起点", category="attraction", latitude=30.1, longitude=120.1)
    to_poi = Poi(id="p2", name="终点", category="attraction", latitude=30.2, longitude=120.2)

    with pytest.raises(ProviderError) as error:
        client.get_route(from_poi, to_poi)

    assert error.value.code == "route_not_found"


def test_rate_limit_is_converted_to_stable_error(tmp_path) -> None:
    client = AmapClient(
        Settings(amap_api_key="test", amap_cache_path=str(tmp_path / "amap-cache.json"))
    )
    calls = 0

    class RateLimitedHttp:
        def get(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                request=httpx.Request("GET", "https://example.test"),
                json={
                    "status": "0",
                    "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT",
                    "infocode": "10021",
                },
            )

    client.http = RateLimitedHttp()

    with pytest.raises(ProviderError) as error:
        client._get("/weather/weatherInfo", city="杭州")
    with pytest.raises(ProviderError) as second_error:
        client._get("/weather/weatherInfo", city="杭州")

    assert error.value.code == "amap_rate_limited"
    assert "频率限制" in error.value.message
    assert second_error.value.code == "amap_rate_limited"
    assert calls == 1


def test_non_object_amap_response_uses_stable_error() -> None:
    client = AmapClient(Settings(amap_api_key="test"))

    class InvalidHttp:
        def get(self, *args, **kwargs):
            return httpx.Response(
                200,
                request=httpx.Request("GET", "https://example.test"),
                json=["invalid"],
            )

    client.http = InvalidHttp()

    with pytest.raises(ProviderError) as error:
        client._get("/place/text", city="杭州", keywords="景点")

    assert error.value.code == "amap_invalid_response"


def test_successful_response_is_reused_from_disk(tmp_path) -> None:
    cache_path = str(tmp_path / "amap-cache.json")
    client = AmapClient(Settings(amap_api_key="test", amap_cache_path=cache_path))
    calls = 0

    class SuccessfulHttp:
        def get(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                request=httpx.Request("GET", "https://example.test"),
                json={"status": "1", "pois": []},
            )

    client.http = SuccessfulHttp()
    first = client._get("/place/text", city="杭州", keywords="景点")
    second = client._get("/place/text", city="杭州", keywords="景点")
    restored = AmapClient(Settings(amap_api_key="", amap_cache_path=cache_path))

    assert first == second == {"status": "1", "pois": []}
    assert restored._get("/place/text", city="杭州", keywords="景点") == first
    assert calls == 1


def test_successful_response_is_reused_from_sqlite(tmp_path) -> None:
    repository = SqliteTripRepository(str(tmp_path / "state.sqlite3"))
    settings = Settings(amap_api_key="test", amap_cache_path=str(tmp_path / "unused.json"))
    client = AmapClient(settings, repository)
    calls = 0

    class SuccessfulHttp:
        def get(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                request=httpx.Request("GET", "https://example.test"),
                json={"status": "1", "pois": []},
            )

    client.http = SuccessfulHttp()
    first = client._get("/place/text", city="杭州", keywords="景点")
    restored = AmapClient(Settings(amap_api_key=""), repository)

    assert restored._get("/place/text", city="杭州", keywords="景点") == first
    assert not (tmp_path / "unused.json").exists()
    assert calls == 1
