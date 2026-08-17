"""把 Locust、Fake Upstream 和 SQLite 指标汇总成中文基线报告。"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


def build_report(results_dir: Path, database_path: Path) -> dict[str, Any]:
    """读取本轮产物并生成稳定的机器可读汇总。"""

    locust = _locust_summary(results_dir / "locust_stats.csv")
    stages = _stage_summary(results_dir / "locust_stats_history.csv")
    failures = _locust_failures(results_dir / "locust_failures.csv")
    application = _application_log(results_dir / "app.log")
    fake = _read_json(results_dir / "fake-stats.json")
    business = _read_json(results_dir / "business-counters.json")
    database = _database_summary(database_path)
    expected_calls = _expected_provider_calls(database, business)
    actual_calls = sum(
        int(item.get("calls", 0))
        for item in fake.get("providers", {}).values()
        if isinstance(item, dict)
    )
    return {
        "scenario": fake.get("scenario", "unknown"),
        "machine": _read_json(results_dir / "machine.json"),
        "http": {**locust, **failures, **application},
        "stages": stages,
        "business": business,
        "database": database,
        "providers": fake.get("providers", {}),
        "reuse": {
            "expected_calls_without_reuse": expected_calls,
            "actual_fake_calls": actual_calls,
            "cache_or_merge_avoided_estimate": max(0, expected_calls - actual_calls),
            "note": "估算值同时包含 SQLite 缓存和进程内同请求合并，不用于计费。",
        },
    }


def write_report(results_dir: Path, database_path: Path) -> dict[str, Any]:
    """写出 JSON 与中文 Markdown 两种结果。"""

    report = build_report(results_dir, database_path)
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (results_dir / "summary.md").write_text(_markdown(report), encoding="utf-8")
    return report


def _locust_summary(path: Path) -> dict[str, Any]:
    rows = _csv_rows(path)
    aggregate = next((row for row in rows if row.get("Name") == "Aggregated"), {})
    return {
        "requests": _integer(aggregate.get("Request Count")),
        "failures": _integer(aggregate.get("Failure Count")),
        "rps": _number(aggregate.get("Requests/s")),
        "p50_ms": _number(aggregate.get("50%") or aggregate.get("Median Response Time")),
        "p95_ms": _number(aggregate.get("95%")),
        "p99_ms": _number(aggregate.get("99%")),
    }


def _locust_failures(path: Path) -> dict[str, int]:
    rows = _csv_rows(path)
    errors = Counter()
    for row in rows:
        message = str(row.get("Error", "")).lower()
        count = _integer(row.get("Occurrences"))
        if "database is locked" in message or "sqlite" in message and "locked" in message:
            errors["sqlite_locked"] += count
        if "timeout" in message or "timed out" in message:
            errors["timeouts"] += count
        if "429" in message or "rate" in message and "limit" in message:
            errors["rate_limited"] += count
    return dict(errors)


def _stage_summary(path: Path) -> list[dict[str, Any]]:
    rows = [row for row in _csv_rows(path) if row.get("Name") == "Aggregated"]
    targets = (10, 50, 200, 500)
    previous_requests = 0
    previous_failures = 0
    summaries: list[dict[str, Any]] = []
    for target in targets:
        stage_rows = [row for row in rows if _integer(row.get("User Count")) == target]
        if not stage_rows:
            continue
        last = stage_rows[-1]
        total_requests = _integer(last.get("Total Request Count"))
        total_failures = _integer(last.get("Total Failure Count"))
        rps_values = [_number(row.get("Requests/s")) for row in stage_rows]
        summaries.append(
            {
                "users": target,
                "samples": len(stage_rows),
                "requests": max(0, total_requests - previous_requests),
                "failures": max(0, total_failures - previous_failures),
                "average_rps": round(sum(rps_values) / len(rps_values), 2),
                "peak_rps": max(rps_values),
                "p50_ms": _number(last.get("50%")),
                "p95_ms": _number(last.get("95%")),
                "p99_ms": _number(last.get("99%")),
            }
        )
        previous_requests = total_requests
        previous_failures = total_failures
    return summaries


def _application_log(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {"unhandled_exceptions": 0, "sqlite_locked": 0, "sqlite_open_errors": 0}
    content = path.read_text(encoding="utf-8", errors="replace").lower()
    return {
        "unhandled_exceptions": content.count("exception in asgi application")
        + content.count("task exception was never retrieved"),
        "sqlite_locked": content.count("database is locked"),
        "sqlite_open_errors": content.count("unable to open database file"),
    }


def _database_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"available": False}
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        sessions = connection.execute(
            "SELECT status, session_json FROM planning_sessions"
        ).fetchall()
        trips = connection.execute("SELECT itinerary_json FROM trips").fetchall()
        dialogues = connection.execute(
            "SELECT COUNT(*) AS count FROM travel_dialogue_sessions"
        ).fetchone()["count"]
        requests = connection.execute(
            "SELECT COUNT(*) AS count FROM travel_dialogue_requests"
        ).fetchone()["count"]
        cache_entries = connection.execute(
            "SELECT COUNT(*) AS count FROM provider_cache"
        ).fetchone()["count"]
    finally:
        connection.close()
    statuses = Counter(str(row["status"]) for row in sessions)
    steps = [
        step
        for row in sessions
        for step in json.loads(row["session_json"]).get("steps", [])
        if isinstance(step, dict)
    ]
    completed_steps = [step for step in steps if step.get("status") == "completed"]
    cache_hits = sum(bool(step.get("cache_hit")) for step in completed_steps)
    durations = [
        int(step["duration_ms"])
        for step in steps
        if isinstance(step.get("duration_ms"), int)
    ]
    planning_ids = [
        json.loads(row["itinerary_json"]).get("planning_session_id") for row in trips
    ]
    duplicates = sum(count - 1 for count in Counter(planning_ids).values() if count > 1)
    return {
        "available": True,
        "planning_sessions": len(sessions),
        "planning_statuses": dict(statuses),
        "assistant_sessions": dialogues,
        "assistant_requests": requests,
        "trips": len(trips),
        "duplicate_trips": duplicates,
        "provider_cache_entries": cache_entries,
        "completed_steps": len(completed_steps),
        "cache_hits": cache_hits,
        "cache_hit_rate": round(cache_hits / len(completed_steps), 4)
        if completed_steps
        else 0,
        "average_step_duration_ms": round(sum(durations) / len(durations), 2)
        if durations
        else None,
    }


def _expected_provider_calls(database: dict[str, Any], business: dict[str, Any]) -> int:
    sessions = int(database.get("planning_sessions", 0))
    assistant_messages = int(business.get("assistant_messages_completed", 0))
    # 无本地 catalog 的压测容器每个发现会话冷启动需要：高德 4、铁路 8、酒店 1、天气 1。
    return sessions * 14 + assistant_messages


def _markdown(report: dict[str, Any]) -> str:
    http = report["http"]
    database = report["database"]
    reuse = report["reuse"]
    machine = report.get("machine", {})
    stage_rows = "\n".join(
        "| {users} | {requests} | {average_rps} | {peak_rps} | {p50_ms} | {p95_ms} | "
        "{p99_ms} | {failures} |".format(**stage)
        for stage in report.get("stages", [])
    )
    machine_text = (
        f"{machine.get('cpu', 'unknown')}，"
        f"{machine.get('logical_processors', '?')} 逻辑处理器，"
        f"{machine.get('memory_gb', '?')} GiB 内存"
    )
    return f"""# OpenZLTravel 并发基线结果

- 场景：`{report['scenario']}`
- 机器：{machine_text}
- Docker：{machine.get('docker_version', 'unknown')}
- 请求总量：{http.get('requests', 0)}
- RPS：{http.get('rps', 0)}
- p50 / p95 / p99：{http.get('p50_ms')} / {http.get('p95_ms')} / {http.get('p99_ms')} ms
- HTTP 失败：{http.get('failures', 0)}
- 未处理异常：{http.get('unhandled_exceptions', 0)}
- SQLite locked：{http.get('sqlite_locked', 0)}
- SQLite 无法打开：{http.get('sqlite_open_errors', 0)}
- 规划会话：{database.get('planning_sessions', 0)}
- 完成行程：{database.get('trips', 0)}
- 重复行程：{database.get('duplicate_trips', 0)}
- 缓存命中率：{database.get('cache_hit_rate', 0)}
- Fake Provider 实际调用：{reuse.get('actual_fake_calls', 0)}
- 缓存/请求合并避免调用估算：{reuse.get('cache_or_merge_avoided_estimate', 0)}

> {reuse.get('note', '')}

## 分阶段快照

| 用户数 | 阶段请求 | 平均 RPS | 峰值 RPS | p50 | p95 | p99 | 失败 |
|---:|---:|---:|---:|---:|---:|---:|---:|
{stage_rows or '| - | - | - | - | - | - | - | - |'}
"""


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {}


def _integer(value: Any) -> int:
    try:
        return int(float(str(value or 0)))
    except ValueError:
        return 0


def _number(value: Any) -> float:
    try:
        return round(float(str(value or 0)), 2)
    except ValueError:
        return 0


def main() -> int:
    """从命令行生成本轮报告。"""

    parser = argparse.ArgumentParser(description="汇总 OpenZLTravel 并发实验结果")
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    report = write_report(args.results, args.database)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
