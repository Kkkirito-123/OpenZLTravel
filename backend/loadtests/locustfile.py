"""OpenZLTravel 的可重复 Locust 用户模型。

所有写请求使用独立会话和唯一幂等键；只有专门的重复提交任务会复用同一个键，
从而区分正常并发与用户双击。默认阶段为 10 → 50 → 200 → 500 用户。
"""

from __future__ import annotations

import json
import os
import random
import time
from collections import Counter
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from gevent import sleep
from locust import HttpUser, LoadTestShape, between, events, task

from loadtests.config import parse_stages, planning_request

DEFAULT_STAGES = "10:30,50:60,200:90,500:120"
RESULTS_DIR = Path(os.getenv("LOADTEST_RESULTS_DIR", "/results"))
FAKE_UPSTREAM_URL = os.getenv("FAKE_UPSTREAM_URL", "http://fake-upstream:9000")


class ExperimentCounters:
    """记录 Locust 默认 HTTP 指标之外的业务终态。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self.values: Counter[str] = Counter()

    def add(self, name: str, amount: int = 1) -> None:
        """增加一个业务计数。"""

        with self._lock:
            self.values[name] += amount

    def snapshot(self) -> dict[str, int]:
        """返回可序列化快照。"""

        with self._lock:
            return dict(self.values)


COUNTERS = ExperimentCounters()


class TravelUser(HttpUser):
    """模拟读取、聊天、规划和重复操作四类真实用户行为。"""

    wait_time = between(0.2, 0.8)

    def on_start(self) -> None:
        """为每个虚拟用户维护独立的会话引用。"""

        self.assistant_sessions: list[str] = []
        self.planning_sessions: list[str] = []

    @task(40)
    def read_status(self) -> None:
        """读取健康状态和已有会话，模拟页面轮询与恢复。"""

        if self.planning_sessions and random.random() < 0.6:
            session_id = random.choice(self.planning_sessions)
            self.client.get(
                f"/api/planning-sessions/{session_id}",
                name="GET /api/planning-sessions/[id]",
            )
            return
        self.client.get("/health", name="GET /health")

    @task(25)
    def assistant_dialogue(self) -> None:
        """创建助手会话并发送两轮需要模型判断的模糊消息。"""

        created = self.client.post(
            "/api/assistant-sessions",
            name="POST /api/assistant-sessions",
        )
        if created.status_code != 201:
            return
        session_id = created.json().get("state", {}).get("session_id")
        if not session_id:
            return
        self._remember(self.assistant_sessions, session_id)
        COUNTERS.add("assistant_sessions_created")
        for content in ("最近工作有点累，想出去散散心", "我更喜欢安静一点的地方"):
            response = self.client.post(
                f"/api/assistant-sessions/{session_id}/messages",
                name="POST /api/assistant-sessions/[id]/messages",
                json={"message_id": str(uuid4()), "content": content},
            )
            if response.status_code != 200:
                break
            COUNTERS.add("assistant_messages_completed")

    @task(25)
    def planning_discovery(self) -> None:
        """创建规划会话并轮询到发现终态。"""

        session_id = self._create_planning(str(uuid4()))
        if not session_id:
            return
        self._remember(self.planning_sessions, session_id)
        self._poll_discovery(session_id)

    @task(10)
    def duplicate_cancel_retry(self) -> None:
        """覆盖双击幂等、取消和失败重试，不把这些操作混入主流程。"""

        choice = random.random()
        if choice < 0.6:
            self._duplicate_submit()
            return
        if choice < 0.9 and self.planning_sessions:
            self._cancel(random.choice(self.planning_sessions))
            return
        if self.planning_sessions:
            self._retry(random.choice(self.planning_sessions))

    def _create_planning(self, idempotency_key: str) -> str | None:
        response = self.client.post(
            "/api/planning-sessions",
            name="POST /api/planning-sessions",
            headers={"Idempotency-Key": idempotency_key},
            json=planning_request(),
        )
        if response.status_code != 202:
            return None
        session_id = response.json().get("session_id")
        if session_id:
            COUNTERS.add("planning_session_responses")
        return session_id

    def _poll_discovery(self, session_id: str) -> None:
        deadline = time.monotonic() + float(os.getenv("LOADTEST_POLL_TIMEOUT_SECONDS", "12"))
        while time.monotonic() < deadline:
            response = self.client.get(
                f"/api/planning-sessions/{session_id}",
                name="GET /api/planning-sessions/[id]",
            )
            if response.status_code != 200:
                return
            status_value = response.json().get("status")
            if status_value in {"awaiting_selection", "completed"}:
                COUNTERS.add("planning_discovery_completed")
                return
            if status_value in {"failed", "cancelled"}:
                COUNTERS.add(f"planning_{status_value}")
                return
            sleep(0.15)
        COUNTERS.add("planning_poll_timeouts")

    def _duplicate_submit(self) -> None:
        key = f"duplicate-{uuid4()}"
        first = self._create_planning(key)
        second = self._create_planning(key)
        if first and second and first == second:
            COUNTERS.add("idempotency_reused")
            self._remember(self.planning_sessions, first)
            return
        COUNTERS.add("idempotency_conflicts")

    def _cancel(self, session_id: str) -> None:
        with self.client.delete(
            f"/api/planning-sessions/{session_id}",
            name="DELETE /api/planning-sessions/[id]",
            catch_response=True,
        ) as response:
            if response.status_code in {204, 409}:
                response.success()
                COUNTERS.add("cancel_attempts")

    def _retry(self, session_id: str) -> None:
        with self.client.post(
            f"/api/planning-sessions/{session_id}/retry",
            name="POST /api/planning-sessions/[id]/retry",
            catch_response=True,
        ) as response:
            if response.status_code in {202, 409}:
                response.success()
                COUNTERS.add("retry_attempts")

    @staticmethod
    def _remember(values: list[str], value: str) -> None:
        values.append(value)
        if len(values) > 20:
            del values[:-20]


class StagedLoadShape(LoadTestShape):
    """按环境变量定义的用户数和持续时间逐级升压。"""

    stages = tuple(parse_stages(os.getenv("LOADTEST_STAGES", DEFAULT_STAGES)))

    def tick(self) -> tuple[int, float] | None:
        """返回当前目标用户数和生成速率，全部阶段结束后停止。"""

        elapsed = self.get_run_time()
        boundary = 0
        for users, duration in self.stages:
            boundary += duration
            if elapsed < boundary:
                return users, max(5.0, users / 5)
        return None


@events.test_start.add_listener
def reset_fake_upstream(**_: Any) -> None:
    """每轮开始前清空 Fake 统计，失败不阻断 Locust 自身启动。"""

    import urllib.request

    try:
        request = urllib.request.Request(f"{FAKE_UPSTREAM_URL}/reset", method="POST")
        urllib.request.urlopen(request, timeout=3).close()
    except OSError:
        COUNTERS.add("fake_reset_failures")


@events.test_stop.add_listener
def write_experiment_counters(**_: Any) -> None:
    """把业务计数和 Fake 统计写入被 Git 忽略的结果目录。"""

    import urllib.request

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "business-counters.json").write_text(
        json.dumps(COUNTERS.snapshot(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        with urllib.request.urlopen(f"{FAKE_UPSTREAM_URL}/stats", timeout=3) as response:
            payload = json.loads(response.read())
    except OSError as error:
        payload = {"error": type(error).__name__}
    (RESULTS_DIR / "fake-stats.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
