"""把 PR 2 暂存在 PostgreSQL 的 Provider 缓存迁移到 Redis。

缓存属于可再生数据，因此本脚本只复制尚未过期的记录；任一 Redis 写入失败时退出，调用方
不得继续删除旧表。业务 app Schema 在复制成功后再执行版本 2 SQL 原子删除旧缓存表。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

from psycopg import Connection, connect
from psycopg.rows import dict_row

from app.config import Settings
from app.coordination import RedisCoordination


def main() -> None:
    """解析连接地址并迁移仍有效的缓存记录。"""

    settings = Settings()
    parser = argparse.ArgumentParser(description="迁移 PostgreSQL Provider 缓存到 Redis")
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--redis-url", default=settings.redis_url)
    args = parser.parse_args()
    if not args.database_url or not args.redis_url:
        raise SystemExit("缺少 DATABASE_URL 或 REDIS_URL")
    coordination = RedisCoordination(args.redis_url, {})
    try:
        count = migrate_provider_cache(args.database_url, coordination)
    finally:
        coordination.close()
    print(f"Provider 缓存迁移完成：{count} 条")


def migrate_provider_cache(
    database_url: str,
    coordination: RedisCoordination,
) -> int:
    """复制尚未过期的 PostgreSQL 缓存，并返回写入数量。"""

    if coordination.readiness() != "ready":
        raise RuntimeError("Redis 尚未就绪")
    with connect(database_url, row_factory=dict_row) as connection:
        if not _cache_table_exists(connection):
            return 0
        rows = connection.execute(
            """
            SELECT provider, cachekey, payloadjson, expiresat
            FROM app.providercache WHERE expiresat > now()
            ORDER BY provider, cachekey
            """
        ).fetchall()
    now = datetime.now(timezone.utc)
    for row in rows:
        ttl = max(1, int((row["expiresat"] - now).total_seconds()))
        coordination.set_cache_strict(
            str(row["provider"]),
            str(row["cachekey"]),
            row["payloadjson"],
            ttl,
        )
    return len(rows)


def _cache_table_exists(connection: Connection[Any]) -> bool:
    row = connection.execute("SELECT to_regclass('app.providercache') AS name").fetchone()
    return bool(row and row["name"])


if __name__ == "__main__":
    main()
