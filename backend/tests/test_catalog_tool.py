"""CatalogTool 的兜底边界与确定性评分测试。"""

from typing import Any

import pytest

from catalog import CatalogTool, DestinationProfile, PostgresCatalogRepository
from domain.models import CandidateCatalog, City
from providers.base import CatalogUnavailableError


class ProfileRepository:
    """为目的地评分提供固定特征。"""

    def __init__(self, profiles: list[DestinationProfile]) -> None:
        self.profiles = profiles

    async def resolve_city(self, destination: str) -> City:
        raise LookupError(destination)

    async def search_candidates(self, city: City) -> CandidateCatalog:
        raise LookupError(city.name)

    async def destination_profiles(
        self, origin: str, region: str
    ) -> list[DestinationProfile]:
        return self.profiles


class FallbackCatalog:
    """记录高德兜底是否被调用。"""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve_city(self, destination: str) -> City:
        self.calls += 1
        return City(name=destination, latitude=30, longitude=120)

    async def search_candidates(self, city: City) -> CandidateCatalog:
        self.calls += 1
        return CandidateCatalog()


class BrokenRepository(ProfileRepository):
    """模拟 PostgreSQL 连接故障。"""

    async def resolve_city(self, destination: str) -> City:
        raise CatalogUnavailableError()


class FakeCursor:
    """返回预置 PostgreSQL 行。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class FakeConnection:
    """按查询顺序提供异步 Cursor。"""

    def __init__(self, results: list[list[dict[str, Any]]]) -> None:
        self.results = results

    async def execute(self, query: str, parameters: tuple[Any, ...]) -> FakeCursor:
        return FakeCursor(self.results.pop(0))


class FakeConnectionContext:
    """模拟 psycopg 异步连接上下文。"""

    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakePool:
    """模拟已打开的异步连接池。"""

    def __init__(self, results: list[list[dict[str, Any]]]) -> None:
        self.connection_value = FakeConnection(results)

    def connection(self, timeout: float) -> FakeConnectionContext:
        return FakeConnectionContext(self.connection_value)


@pytest.mark.asyncio
async def test_catalog_falls_back_only_when_city_is_not_covered() -> None:
    fallback = FallbackCatalog()
    tool = CatalogTool(ProfileRepository([]), fallback)

    city = await tool.resolve_city("杭州")

    assert city.name == "杭州"
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_catalog_outage_does_not_expand_into_amap_requests() -> None:
    fallback = FallbackCatalog()
    tool = CatalogTool(BrokenRepository([]), fallback)

    with pytest.raises(CatalogUnavailableError):
        await tool.resolve_city("杭州")

    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_postgres_catalog_converts_rows_to_stable_facts() -> None:
    pool = FakePool(
        [
            [
                {
                    "name": "杭州市",
                    "adcode": "330100000000",
                    "latitude": 30.2,
                    "longitude": 120.1,
                }
            ],
            [
                {
                    "locationid": "stable-uuid",
                    "canonicalname": "西湖",
                    "address": "杭州",
                    "category": "attraction",
                    "typename": "湖泊;风景名胜",
                    "imageurl": "https://example.com/west-lake.jpg",
                    "latitude": 30.25,
                    "longitude": 120.15,
                }
            ],
        ]
    )
    repository = PostgresCatalogRepository("", pool=pool)

    city = await repository.resolve_city("杭州")
    catalog = await repository.search_candidates(city)

    assert city.adcode == "330100000000"
    assert catalog.attractions[0].id == "poi:catalog:stable-uuid"
    assert catalog.attractions[0].tags == ["湖泊", "风景名胜"]


@pytest.mark.asyncio
async def test_destination_ranking_uses_fixed_weights_and_excludes_origin() -> None:
    profiles = [
        DestinationProfile(
            city=City(name="上海", adcode="310000", latitude=31.2, longitude=121.4),
            attraction_count=12,
            restaurant_count=12,
            hotel_count=8,
            type_names=("博物馆", "历史遗址"),
            distance_km=0,
        ),
        DestinationProfile(
            city=City(name="苏州", adcode="320500", latitude=31.3, longitude=120.6),
            attraction_count=12,
            restaurant_count=12,
            hotel_count=8,
            type_names=("博物馆", "园林"),
            distance_km=100,
        ),
        DestinationProfile(
            city=City(name="黄山", adcode="341000", latitude=29.7, longitude=118.3),
            attraction_count=6,
            restaurant_count=4,
            hotel_count=4,
            type_names=("山峰",),
            distance_km=450,
        ),
    ]
    tool = CatalogTool(ProfileRepository(profiles))

    candidates = await tool.recommend_destinations("上海", "华东", ["博物馆"], limit=5)

    assert [item.city.name for item in candidates] == ["苏州", "黄山"]
    assert candidates[0].score == 1
    assert candidates[0].candidate_id.startswith("destination:")
    assert "匹配偏好" in "".join(candidates[0].reasons)
