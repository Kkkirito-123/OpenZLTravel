"""SQLite 业务仓库连接生命周期回归测试。"""

import sqlite3
from pathlib import Path
from typing import Any

from tests import sqlite_repository
from tests.sqlite_repository import SqliteTripRepository


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

    monkeypatch.setattr(sqlite_repository.sqlite3, "connect", connect)
    return connections


def test_trip_repository_closes_every_connection(monkeypatch: Any, tmp_path: Path) -> None:
    connections = _track_connections(monkeypatch)
    repository = SqliteTripRepository(str(tmp_path / "trips.sqlite3"))

    assert repository.list() == []

    assert connections
    assert all(connection.closed for connection in connections)
