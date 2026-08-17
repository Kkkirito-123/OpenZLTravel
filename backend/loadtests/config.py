"""Locust 和单元测试共享的纯配置函数，不导入 gevent 或 Locust。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def parse_stages(value: str) -> list[tuple[int, int]]:
    """解析 `用户数:秒数` 列表，并拒绝空或非正阶段。"""

    stages: list[tuple[int, int]] = []
    for raw in value.split(","):
        users, duration = raw.strip().split(":", 1)
        parsed = int(users), int(duration)
        if min(parsed) <= 0:
            raise ValueError("压测阶段必须使用正整数")
        stages.append(parsed)
    if not stages:
        raise ValueError("至少需要一个压测阶段")
    return stages


def planning_request() -> dict[str, Any]:
    """生成始终位于未来且不超过七天的稳定请求。"""

    start = date.today() + timedelta(days=14)
    return {
        "origin": "上海",
        "destination": "杭州",
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=2)).isoformat(),
        "travelers": 2,
        "budget": 5000,
        "pace": "适中",
        "hotel_level": "舒适",
        "transport_mode": "auto",
        "preferences": ["历史", "人文"],
        "dietary_preferences": [],
        "notes": "并发实验室请求",
    }
