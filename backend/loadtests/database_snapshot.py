"""输出并发实验结束时的 PostgreSQL 与 Redis 业务统计。"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from psycopg import connect
from psycopg.rows import dict_row
from redis import Redis

from app.config import Settings


def collect_database_stats(database_url: str, redis_url: str) -> dict[str, Any]:
    """汇总任务终态、重复行程、步骤耗时和 Redis Provider 缓存数量。"""

    with connect(database_url, row_factory=dict_row) as connection:
        sessions = connection.execute(
            "SELECT status, sessionjson FROM app.planningsession"
        ).fetchall()
        trips = connection.execute("SELECT itineraryjson FROM app.trip").fetchall()
        dialogues = _count(connection, "app.dialoguesession")
        requests = _count(connection, "app.dialoguerequest")
    steps = [
        step
        for row in sessions
        for step in row["sessionjson"].get("steps", [])
        if isinstance(step, dict)
    ]
    completed_steps = [step for step in steps if step.get("status") == "completed"]
    cache_hits = sum(bool(step.get("cache_hit")) for step in completed_steps)
    durations = [
        int(step["duration_ms"])
        for step in steps
        if isinstance(step.get("duration_ms"), int)
    ]
    planning_ids = [row["itineraryjson"].get("planning_session_id") for row in trips]
    duplicates = sum(count - 1 for count in Counter(planning_ids).values() if count > 1)
    return {
        "available": True,
        "planning_sessions": len(sessions),
        "planning_statuses": dict(Counter(str(row["status"]) for row in sessions)),
        "assistant_sessions": dialogues,
        "assistant_requests": requests,
        "trips": len(trips),
        "duplicate_trips": duplicates,
        "provider_cache_entries": _redis_cache_count(redis_url),
        "completed_steps": len(completed_steps),
        "cache_hits": cache_hits,
        "cache_hit_rate": round(cache_hits / len(completed_steps), 4)
        if completed_steps
        else 0,
        "average_step_duration_ms": round(sum(durations) / len(durations), 2)
        if durations
        else None,
    }


def _count(connection: Any, table: str) -> int:
    row = connection.execute(f"SELECT count(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def _redis_cache_count(redis_url: str) -> int:
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        return sum(1 for _ in client.scan_iter(match="travel:provider:*:cache:*"))
    finally:
        client.close()


def main() -> None:
    """从环境配置读取连接并输出 JSON。"""

    settings = Settings()
    print(
        json.dumps(
            collect_database_stats(settings.database_url, settings.redis_url),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
