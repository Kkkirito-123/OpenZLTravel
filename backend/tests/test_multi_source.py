"""多来源交通和天气策略测试。"""

import asyncio
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.errors import CatalogUnavailableError, ProviderError
from app.models import City, Poi
from app.providers import AmapScheduler, HybridMapProvider, OpenMeteoClient
from app.storage import SqliteTripRepository
from app.travel import TravelService
from tests.fakes import FakeMapProvider, FakePlanner, sample_catalog, sample_request


def poi(poi_id: str, latitude: float = 30.1, longitude: float = 120.1) -> Poi:
    return Poi(
        id=poi_id,
        name=poi_id,
        category="attraction",
        latitude=latitude,
        longitude=longitude,
    )


def test_open_meteo_parses_wmo_codes() -> None:
    client = OpenMeteoClient(Settings())
    client.http = _ResponseClient(
        {
            "daily": {
                "time": ["2026-09-01"],
                "weather_code": [80],
                "temperature_2m_max": [27.4],
                "temperature_2m_min": [19.2],
            }
        }
    )

    result = client.get_weather(
        City(name="测试市", latitude=30.1, longitude=120.1),
        date(2026, 9, 1),
        date(2026, 9, 1),
    )

    assert result[0].day_weather == "阵雨"
    assert result[0].source and result[0].source.provider == "open_meteo"


def test_open_meteo_reuses_sqlite_cache(tmp_path: Path) -> None:
    repository = SqliteTripRepository(str(tmp_path / "weather.sqlite3"))
    city = City(name="测试市", latitude=30.1, longitude=120.1)
    first = OpenMeteoClient(Settings(), repository)
    first.http = _ResponseClient(
        {
            "daily": {
                "time": ["2026-09-01"],
                "weather_code": [0],
                "temperature_2m_max": [27],
                "temperature_2m_min": [19],
            }
        }
    )
    first_result = first.get_weather(city, date(2026, 9, 1), date(2026, 9, 1))

    restored = OpenMeteoClient(Settings(), repository)
    restored.http = _FailingResponseClient()
    second_result = restored.get_weather(city, date(2026, 9, 1), date(2026, 9, 1))

    assert first_result == second_result


def test_open_meteo_success_skips_amap_weather() -> None:
    upstream = CountingMapProvider()
    provider = HybridMapProvider(
        catalog=EmptyCatalog(),
        upstream=upstream,
        weather_provider=StaticWeatherProvider(),
    )

    result = provider.get_weather(City(name="测试市"), date(2026, 9, 1), date(2026, 9, 1))

    assert result[0].source and result[0].source.provider == "open_meteo"
    assert upstream.weather_calls == 0


def test_open_meteo_failure_falls_back_to_amap_weather() -> None:
    upstream = CountingMapProvider()
    provider = HybridMapProvider(
        catalog=EmptyCatalog(),
        upstream=upstream,
        weather_provider=FailedWeatherProvider(),
    )

    provider.get_weather(City(name="测试市"), date(2026, 9, 1), date(2026, 9, 1))

    assert upstream.weather_calls == 1


@pytest.mark.asyncio
async def test_local_driving_does_not_call_amap() -> None:
    upstream = CountingMapProvider()
    provider = HybridMapProvider(EmptyCatalog(), upstream)

    result = await provider.get_transport_async(
        City(name="测试市"), [poi("a1"), poi("a2", 30.2, 120.2)], "driving"
    )

    assert result.routes[0].source and result.routes[0].source.provider == "local_estimate"
    assert result.routes[0].polyline == []
    assert upstream.route_calls == 0


@pytest.mark.asyncio
async def test_realtime_driving_uses_one_waypoint_request() -> None:
    upstream = CountingMapProvider()
    provider = HybridMapProvider(EmptyCatalog(), upstream)
    points = [poi("a1"), poi("a2", 30.2, 120.2), poi("a3", 30.3, 120.3)]

    result = await provider.get_transport_async(City(name="测试市"), points, "realtime_driving")

    assert result.routes[0].via_poi_ids == ["a2"]
    assert upstream.waypoint_calls == 1


@pytest.mark.asyncio
async def test_transit_failure_falls_back_to_local_estimate() -> None:
    upstream = CountingMapProvider(transit_error=True)
    provider = HybridMapProvider(EmptyCatalog(), upstream)

    result = await provider.get_transport_async(
        City(name="测试市"), [poi("a1"), poi("a2", 30.2, 120.2)], "transit"
    )

    assert result.routes[0].mode == "步行估算"
    assert "公交路线暂时不可用" in result.warnings[0]


@pytest.mark.asyncio
async def test_scheduler_deduplicates_concurrent_requests() -> None:
    scheduler = AmapScheduler(concurrency=2, min_interval_seconds=0)
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "ok"

    assert await asyncio.gather(*[scheduler.run("same", operation) for _ in range(5)]) == ["ok"] * 5
    assert calls == 1


@pytest.mark.asyncio
async def test_scheduler_keeps_shared_request_when_owner_is_cancelled() -> None:
    """取消一个页面请求不能取消其他页面正在等待的同一高德查询。"""

    scheduler = AmapScheduler(concurrency=1, min_interval_seconds=0)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "ok"

    owner = asyncio.create_task(scheduler.run("same", operation))
    await started.wait()
    follower = asyncio.create_task(scheduler.run("same", operation))
    await asyncio.sleep(0)

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert "same" in scheduler.inflight

    release.set()
    assert await follower == "ok"
    await asyncio.sleep(0)
    assert calls == 1
    assert scheduler.inflight == {}


@pytest.mark.asyncio
async def test_local_catalog_skips_amap_scheduler_for_city_and_candidates() -> None:
    """公开目录命中时不应为高德调度间隔额外等待。"""

    class LocalCatalog:
        available = True

        def resolve_city(self, destination: str) -> City:
            assert destination == "测试市"
            return City(name="测试市", latitude=30.1, longitude=120.1)

        def search_candidates(self, city: City):
            assert city.name == "测试市"
            return sample_catalog()

    class UnexpectedUpstream(FakeMapProvider):
        def resolve_city(self, destination: str) -> City:
            raise AssertionError(f"不应请求高德城市查询：{destination}")

        def search_candidates(self, city: City):
            raise AssertionError(f"不应请求高德 POI 查询：{city.name}")

    class CountingScheduler(AmapScheduler):
        def __init__(self) -> None:
            super().__init__(min_interval_seconds=0)
            self.calls = 0

        async def run(self, *args, **kwargs):
            self.calls += 1
            return await super().run(*args, **kwargs)

    scheduler = CountingScheduler()
    provider = HybridMapProvider(LocalCatalog(), UnexpectedUpstream(), scheduler=scheduler)

    city = await provider.resolve_city_async("测试市")
    candidates = await provider.search_candidates_async(city)

    assert candidates.attractions
    assert scheduler.calls == 0


@pytest.mark.asyncio
async def test_catalog_failure_does_not_fall_back_to_amap() -> None:
    """数据库故障时必须停止，避免并发请求被放大为高德流量。"""

    class FailedCatalog:
        available = True

        def resolve_city(self, destination: str) -> City:
            del destination
            raise CatalogUnavailableError()

    class UnexpectedUpstream(FakeMapProvider):
        def resolve_city(self, destination: str) -> City:
            raise AssertionError(f"数据库故障时不应请求高德：{destination}")

    provider = HybridMapProvider(FailedCatalog(), UnexpectedUpstream())

    with pytest.raises(CatalogUnavailableError):
        await provider.resolve_city_async("测试市")


@pytest.mark.asyncio
async def test_langgraph_workflow_saves_once_after_validation(tmp_path: Path) -> None:
    provider = AsyncLocalProvider()
    service = TravelService(
        provider,
        FakePlanner(),
        SqliteTripRepository(str(tmp_path / "trips.sqlite3")),
    )

    result = await service.create_async(sample_request())

    assert result.days[0].routes == []
    assert len(service.list()) == 1
    assert provider.route_calls == 0


class _ResponseClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def get(self, *args: object, **kwargs: object) -> httpx.Response:
        del args, kwargs
        return httpx.Response(
            200,
            request=httpx.Request("GET", "https://example.test"),
            json=self.payload,
        )


class _FailingResponseClient:
    def get(self, *args: object, **kwargs: object) -> httpx.Response:
        del args, kwargs
        raise AssertionError("命中 SQLite 缓存时不应请求 Open-Meteo")


class EmptyCatalog:
    available = False


class StaticWeatherProvider:
    def get_weather(self, city, start_date, end_date):
        del city, end_date
        from app.models import DataSource, WeatherDay

        return [
            WeatherDay(
                date=start_date,
                day_weather="晴",
                source=DataSource(provider="open_meteo", freshness="forecast"),
            )
        ]


class FailedWeatherProvider:
    def get_weather(self, city, start_date, end_date):
        del city, start_date, end_date
        raise ProviderError("weather_unavailable", "测试天气失败")


class AsyncLocalProvider(FakeMapProvider):
    async def resolve_city_async(self, destination):
        return self.resolve_city(destination)

    async def search_candidates_async(self, city):
        return self.search_candidates(city)

    async def get_weather_async(self, city, start_date, end_date):
        return self.get_weather(city, start_date, end_date)

    async def get_transport_async(self, city, day_pois, mode):
        del city
        from app.providers import TransportResult, local_routes

        return TransportResult(local_routes(day_pois, mode), [])


class CountingMapProvider(FakeMapProvider):
    def __init__(self, transit_error: bool = False) -> None:
        super().__init__(sample_catalog())
        self.weather_calls = 0
        self.waypoint_calls = 0
        self.transit_error = transit_error

    def get_weather(self, city, start_date, end_date):
        self.weather_calls += 1
        return super().get_weather(city, start_date, end_date)

    def get_route_with_waypoints(self, day_pois):
        self.waypoint_calls += 1
        from app.models import DataSource, RouteSegment

        return RouteSegment(
            from_poi_id=day_pois[0].id,
            to_poi_id=day_pois[-1].id,
            via_poi_ids=[item.id for item in day_pois[1:-1]],
            distance_km=5,
            duration_minutes=20,
            mode="实时驾车",
            source=DataSource(provider="amap", freshness="realtime"),
        )

    def get_transit(self, city, from_poi, to_poi):
        del city, from_poi, to_poi
        if self.transit_error:
            raise ProviderError("route_not_found", "无公交方案")
        raise AssertionError("测试不应进入成功公交分支")
