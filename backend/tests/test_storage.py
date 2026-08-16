"""SQLite 连接生命周期与目录仓库回归测试。"""

import sqlite3
from pathlib import Path
from typing import Any

from app import storage
from app.storage import CatalogRepository, SqliteTripRepository


class TrackingConnection(sqlite3.Connection):
    """记录 close 调用，避免依赖垃圾回收时机判断资源是否释放。"""

    closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


def _track_connections(monkeypatch: Any) -> list[TrackingConnection]:
    """替换本模块的连接工厂，并返回本次测试创建的全部连接。"""

    original_connect = sqlite3.connect
    connections: list[TrackingConnection] = []

    def connect(*args: Any, **kwargs: Any) -> TrackingConnection:
        kwargs["factory"] = TrackingConnection
        connection = original_connect(*args, **kwargs)
        assert isinstance(connection, TrackingConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(storage.sqlite3, "connect", connect)
    return connections


def test_trip_repository_closes_every_connection(monkeypatch: Any, tmp_path: Path) -> None:
    connections = _track_connections(monkeypatch)
    repository = SqliteTripRepository(str(tmp_path / "trips.sqlite3"))

    assert repository.list() == []

    assert connections
    assert all(connection.closed for connection in connections)


def test_catalog_repository_closes_query_connection(monkeypatch: Any, tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE cities (
                city_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                latitude REAL,
                longitude REAL,
                population INTEGER NOT NULL
            );
            CREATE TABLE city_aliases (
                alias TEXT NOT NULL,
                city_id INTEGER NOT NULL,
                population INTEGER NOT NULL
            );
            INSERT INTO cities VALUES (1, '杭州市', 30.27, 120.15, 12000000);
            INSERT INTO city_aliases VALUES ('杭州', 1, 12000000);
            """
        )
        connection.commit()
    finally:
        connection.close()
    connections = _track_connections(monkeypatch)

    city = CatalogRepository(str(database)).resolve_city("杭州")

    assert city.name == "杭州市"
    assert connections
    assert all(item.closed for item in connections)
