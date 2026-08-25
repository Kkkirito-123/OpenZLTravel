"""PostGIS 的 Windows 同步连接兼容层。"""

from __future__ import annotations

from typing import Any

from psycopg import connect
from psycopg.rows import dict_row


def execute_sync(
    database_url: str,
    query: str,
    parameters: tuple[Any, ...],
    timeout_seconds: float,
) -> list[Any]:
    """在线程中执行一次查询，绕过 Proactor 与 psycopg 异步层的冲突。"""

    with connect(
        database_url,
        connect_timeout=max(1, round(timeout_seconds)),
        row_factory=dict_row,
    ) as connection:
        cursor = connection.execute(query, parameters)
        return list(cursor.fetchall())
