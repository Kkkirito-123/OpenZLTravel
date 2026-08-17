"""PostgreSQL 运行时地点目录测试。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from uuid import UUID

import pytest
from psycopg import OperationalError

from app.catalog import PostgresCatalogRepository, normalize_location_name
from app.errors import CatalogUnavailableError


class FakeResult:
    """返回预设字典行的最小查询结果。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        """返回全部测试记录。"""

        return self.rows


class FakeConnection:
    """记录 SQL 和参数，并按查询类型返回测试事实。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, query: str, parameters: tuple[Any, ...]) -> FakeResult:
        """执行测试查询。"""

        self.calls.append((query, parameters))
        if "catalog.locationname" in query:
            return FakeResult(
                [
                    {
                        "name": "西安市",
                        "adcode": "610100000000",
                        "latitude": 34.34,
                        "longitude": 108.94,
                    }
                ]
            )
        if "WITH center" in query:
            return FakeResult(_poi_rows())
        return FakeResult([{"count": 1}])


class FakePool:
    """模拟 psycopg 连接池的上下文接口。"""

    def __init__(self) -> None:
        self.connection_object = FakeConnection()
        self.closed = False

    @contextmanager
    def connection(self, timeout: float) -> Any:
        """返回可复用的测试连接。"""

        assert timeout == 3
        yield self.connection_object

    def get_stats(self) -> dict[str, int]:
        """返回不含连接信息的池统计。"""

        return {"pool_size": 2, "pool_available": 1, "requests_waiting": 0}

    def close(self) -> None:
        """记录关闭动作。"""

        self.closed = True


class FailedPool:
    """模拟数据库连接失败。"""

    def connection(self, timeout: float) -> Any:
        """在借用连接时抛出数据库错误。"""

        del timeout
        raise OperationalError("database unavailable")

    def get_stats(self) -> dict[str, int]:
        """返回空池统计。"""

        return {}

    def close(self) -> None:
        """保持与真实池相同的关闭接口。"""


def test_normalize_location_name_matches_catalog_builder() -> None:
    assert normalize_location_name(" Xi an / 西 安 ") == "xian西安"


def test_postgres_catalog_resolves_alias_and_returns_three_categories() -> None:
    pool = FakePool()
    repository = PostgresCatalogRepository("postgresql://test", pool=pool)

    city = repository.resolve_city("xi an")
    catalog = repository.search_candidates(city)

    assert city.name == "西安市"
    assert city.adcode == "610100000000"
    assert pool.connection_object.calls[0][1] == ("xian",)
    assert len(catalog.attractions) == len(catalog.restaurants) == len(catalog.hotels) == 1
    assert catalog.attractions[0].id == "11111111-1111-1111-1111-111111111111"
    assert catalog.hotels[0].image_url == "https://example.test/hotel.jpg"


def test_postgres_catalog_reports_pool_status_and_closes() -> None:
    pool = FakePool()
    repository = PostgresCatalogRepository("postgresql://test", pool=pool)

    status = repository.readiness()
    repository.close()

    assert status == {
        "status": "ready",
        "pool": {"pool_size": 2, "pool_available": 1, "requests_waiting": 0},
    }
    assert pool.closed


def test_postgres_catalog_failure_uses_stable_error() -> None:
    repository = PostgresCatalogRepository("postgresql://test", pool=FailedPool())

    with pytest.raises(CatalogUnavailableError) as captured:
        repository.resolve_city("西安")

    assert captured.value.code == "catalog_unavailable"


def test_postgres_catalog_missing_configuration_is_not_amap_fallback() -> None:
    repository = PostgresCatalogRepository("")

    assert repository.available
    with pytest.raises(CatalogUnavailableError):
        repository.resolve_city("西安")


def _poi_rows() -> list[dict[str, object]]:
    return [
        _poi_row("11111111-1111-1111-1111-111111111111", "城墙", "attraction"),
        _poi_row("22222222-2222-2222-2222-222222222222", "餐厅", "restaurant"),
        _poi_row(
            "33333333-3333-3333-3333-333333333333",
            "酒店",
            "hotel",
            "https://example.test/hotel.jpg",
        ),
    ]


def _poi_row(
    location_id: str, name: str, category: str, image_url: str | None = None
) -> dict[str, object]:
    return {
        "locationid": UUID(location_id),
        "canonicalname": name,
        "address": "测试地址",
        "category": category,
        "typename": "test",
        "imageurl": image_url,
        "latitude": 34.3,
        "longitude": 108.9,
    }
