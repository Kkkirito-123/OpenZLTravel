from datetime import date

from app.config import Settings
from app.models import City, Poi
from app.providers import AmapClient


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

    assert city == City(name="测试市", adcode="123", latitude=30.1, longitude=120.1)
    assert catalog.attractions[0].id == "p1"
    assert catalog.attractions[0].latitude == 30.2
    assert weather[0].day_weather == "晴"


def test_empty_weather_response_is_safe() -> None:
    client = AmapClient(Settings(amap_api_key="test"))
    client._get = lambda path, **params: {"status": "1", "forecasts": []}

    assert client.get_weather(City(name="测试市"), date(2026, 9, 1), date(2026, 9, 1)) == []


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
    assert route.polyline[0].latitude == 30.1
