"""Redis 多 Worker 协调能力测试。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

import fakeredis
import pytest
from fakeredis.aioredis import FakeRedis as AsyncFakeRedis
from pydantic import BaseModel

from app.coordination import LeaseHandle, RedisCoordination
from app.errors import (
    CoordinationUnavailableError,
    RateLimitExceededError,
)
from app.providers.base import ProviderExecutor
from app.providers.rail import RailProvider
from app.runtime import PlanningRuntime


class CachedModel(BaseModel):
    """验证 Redis 缓存不会把领域模型退化成字符串。"""

    item_id: str
    fetched_at: datetime


def make_coordination(
    server: fakeredis.FakeServer,
    *,
    limits: dict[str, int] | None = None,
    api_limit: int = 2,
) -> RedisCoordination:
    """让同步与异步客户端共享同一内存 Redis。"""

    coordination = RedisCoordination(
        "",
        limits or {"rail": 1},
        task_lease_renew_seconds=60,
        api_rate_limit_per_minute=api_limit,
    )
    coordination._sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    coordination._async = AsyncFakeRedis(server=server, decode_responses=True)
    return coordination


def test_cache_visitor_and_idempotency_share_state() -> None:
    """多个 Worker 必须看到相同缓存、访客和幂等提示。"""

    server = fakeredis.FakeServer()
    first = make_coordination(server)
    second = make_coordination(server)
    visitor_id = uuid4()
    session_id = uuid4()

    first.set_cache("rail", "same-query", {"ok": True}, 60)
    first.set_visitor("a" * 64, visitor_id)
    first.set_idempotency(visitor_id, "same-request", session_id)

    assert second.get_cache("rail", "same-query") == {"ok": True}
    assert second.get_visitor("a" * 64) == visitor_id
    assert second.get_idempotency(visitor_id, "same-request") == session_id


def test_cache_preserves_pydantic_model_structure() -> None:
    """Pydantic 模型、日期和列表写入后必须仍是可校验的 JSON 结构。"""

    server = fakeredis.FakeServer()
    coordination = make_coordination(server)
    value = CachedModel(
        item_id="rail-1",
        fetched_at=datetime(2026, 8, 17, 10, 30, tzinfo=timezone.utc),
    )

    coordination.set_cache("rail", "model", [value], 60)

    cached = coordination.get_cache("rail", "model")
    assert isinstance(cached, list)
    restored = CachedModel.model_validate(cached[0])
    assert restored == value


@pytest.mark.asyncio
async def test_session_lock_uses_token_safe_release() -> None:
    """释放锁必须比较随机 Token，旧 Worker 不能删除新 Worker 的锁。"""

    server = fakeredis.FakeServer()
    coordination = make_coordination(server)
    session_id = uuid4()
    key = f"travel:session:{session_id}:lock"

    async with coordination.session_lock(session_id):
        assert await coordination._async.get(key)
    assert await coordination._async.get(key) is None

    await coordination._async.set(key, "new-owner", px=30_000)
    await coordination._release_string_lock(key, "old-owner")
    assert await coordination._async.get(key) == "new-owner"


@pytest.mark.asyncio
async def test_session_lock_queues_same_worker_before_redis_wait() -> None:
    """同一 Worker 的并行步骤应先本地排队，不能一起消耗 Redis 等待窗口。"""

    coordination = make_coordination(fakeredis.FakeServer())
    session_id = uuid4()
    acquired_calls = 0
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    original_acquire = coordination._acquire_lock

    async def counted_acquire(*args: Any, **kwargs: Any) -> None:
        nonlocal acquired_calls
        acquired_calls += 1
        await original_acquire(*args, **kwargs)

    coordination._acquire_lock = counted_acquire  # type: ignore[method-assign]

    async def first() -> None:
        async with coordination.session_lock(session_id):
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        async with coordination.session_lock(session_id):
            return

    first_task = asyncio.create_task(first())
    await first_entered.wait()
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert acquired_calls == 1

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert acquired_calls == 2


@pytest.mark.asyncio
async def test_provider_limit_is_global_and_released() -> None:
    """铁路并发上限为一时，第二个 Worker 不能同时取得槽位。"""

    server = fakeredis.FakeServer()
    first = make_coordination(server)
    second = make_coordination(server)
    key = "travel:provider:rail:slots"

    async with first.provider_slot("rail"):
        with pytest.raises(RateLimitExceededError):
            await second._acquire_slot(key, "second", 1, wait_seconds=0)
    async with second.provider_slot("rail"):
        assert await second._async.zcard(key) == 1


@pytest.mark.asyncio
async def test_same_provider_request_runs_once_across_workers() -> None:
    """不同 Worker 同时查询相同缓存键时，只允许一个真实回源操作。"""

    server = fakeredis.FakeServer()
    first_coordination = make_coordination(server, limits={"hotel": 2})
    second_coordination = make_coordination(server, limits={"hotel": 2})
    first = ProviderExecutor(
        "hotel", first_coordination, coordination=first_coordination
    )
    second = ProviderExecutor(
        "hotel", second_coordination, coordination=second_coordination
    )
    calls = 0

    async def operation() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"ok": True}

    results = await asyncio.gather(
        first.run("same-query", 60, operation),
        second.run("same-query", 60, operation),
    )

    assert calls == 1
    assert [cache_hit for _, cache_hit in results].count(True) == 1


@pytest.mark.asyncio
async def test_rail_query_does_not_reenter_single_global_slot() -> None:
    """车站解析与车票查询必须分阶段占槽，12306 并发一时仍能完成。"""

    class RailClient:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if name == "search-stations":
                return {
                    "success": True,
                    "stations": [{"name": arguments["query"], "code": "TST"}],
                }
            if name == "query-tickets":
                return {
                    "success": True,
                    "trains": [
                        {
                            "train_no": "G1",
                            "from_station": "测试站",
                            "to_station": "目标站",
                            "start_time": "08:00",
                            "arrive_time": "09:00",
                            "duration": "01:00",
                            "seats": {"second_class": "有"},
                        }
                    ],
                }
            return {"data": [{"train_code": "G1", "prices": {"二等座": "100"}}]}

    coordination = make_coordination(fakeredis.FakeServer(), limits={"rail": 1})
    executor = ProviderExecutor("rail", coordination, coordination=coordination)
    provider = RailProvider(RailClient(), executor)

    options, cache_hit = await provider.search(
        "出发地", "目的地", date(2026, 9, 1), "outbound"
    )

    assert cache_hit is False
    assert options and options[0].train_code == "G1"


@pytest.mark.asyncio
async def test_task_lease_allows_takeover_after_release() -> None:
    """同一任务只有一个持有者，前一 Worker 退出后其他 Worker 可以接管。"""

    server = fakeredis.FakeServer()
    first = make_coordination(server)
    second = make_coordination(server)
    session_id = uuid4()

    async with first.task_lease(session_id) as first_lease:
        assert first_lease is not None
        async with second.task_lease(session_id) as blocked:
            assert blocked is None
    async with second.task_lease(session_id) as takeover:
        assert takeover is not None


@pytest.mark.asyncio
async def test_redis_failure_follows_fail_open_and_fail_close_rules() -> None:
    """普通缓存与 API 限流放行，锁和 Provider 保护必须拒绝执行。"""

    coordination = RedisCoordination("", {"rail": 1})

    assert coordination.get_cache("rail", "query") is None
    assert await coordination.allow_request("visitor") is True
    with pytest.raises(CoordinationUnavailableError):
        async with coordination.session_lock(uuid4()):
            pass
    with pytest.raises(CoordinationUnavailableError):
        async with coordination.provider_slot("rail"):
            pass


@pytest.mark.asyncio
async def test_api_rate_limit_uses_shared_counter() -> None:
    """相同访客跨 Worker 共用一分钟请求计数。"""

    server = fakeredis.FakeServer()
    first = make_coordination(server, api_limit=2)
    second = make_coordination(server, api_limit=2)

    assert await first.allow_request("visitor") is True
    assert await second.allow_request("visitor") is True
    assert await first.allow_request("visitor") is False


@pytest.mark.asyncio
async def test_api_rate_limit_fails_open_for_closed_event_loop_client() -> None:
    """热重载遗留的异步 Redis 连接不能阻断 API 请求。"""

    class ClosedLoopClient:
        async def incr(self, key: str) -> int:
            del key
            raise RuntimeError("Event loop is closed")

    coordination = RedisCoordination("", {"rail": 1})
    coordination._async = ClosedLoopClient()

    assert await coordination.allow_request("visitor") is True


@pytest.mark.asyncio
async def test_runtime_cancels_operation_when_task_lease_is_lost() -> None:
    """续租失败后必须停止当前任务，避免两个 Worker 同时推进状态。"""

    class LosingCoordination:
        @asynccontextmanager
        async def task_lease(self, session_id: Any):
            del session_id
            lost = asyncio.Event()
            asyncio.get_running_loop().call_later(0.01, lost.set)
            yield LeaseHandle("test", lost)

    runtime = PlanningRuntime.__new__(PlanningRuntime)
    runtime.coordination = LosingCoordination()
    cancelled = asyncio.Event()

    async def operation() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    await runtime._run_with_lease(uuid4(), operation)

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_runtime_cancels_inner_operation_when_outer_task_is_cancelled() -> None:
    """进程关闭或会话取消时，租约包装层不能遗留孤儿任务。"""

    class StableCoordination:
        @asynccontextmanager
        async def task_lease(self, session_id: Any):
            del session_id
            yield LeaseHandle("test", asyncio.Event())

    runtime = PlanningRuntime.__new__(PlanningRuntime)
    runtime.coordination = StableCoordination()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def operation() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(runtime._run_with_lease(uuid4(), operation))
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert cancelled.is_set()
