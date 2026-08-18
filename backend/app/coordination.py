"""Redis 多 Worker 协调能力。

本模块集中缓存、访客缓存、幂等提示、会话锁、Provider 全局并发和后台任务租约。
业务模块只使用语义方法，不能自行拼接 Redis Key。普通缓存故障允许回源；会话锁、
Provider 槽和任务租约故障必须拒绝执行，避免多个 Worker 同时写状态或放大上游流量。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import weakref
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from app.errors import (
    CoordinationUnavailableError,
    RateLimitExceededError,
    SessionBusyError,
)

LOGGER = logging.getLogger("openzltravel.coordination")

_COMPARE_DELETE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
_COMPARE_EXPIRE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
_ACQUIRE_SLOT = """
redis.call('zremrangebyscore', KEYS[1], '-inf', ARGV[1])
if redis.call('zcard', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('zadd', KEYS[1], ARGV[3], ARGV[4])
  redis.call('pexpire', KEYS[1], ARGV[5])
  return 1
end
return 0
"""


@dataclass
class LeaseHandle:
    """表示一个带续租监测的后台任务租约。"""

    token: str
    lost: asyncio.Event

    async def wait_lost(self) -> None:
        """等待租约丢失或 Redis 续租失败。"""

        await self.lost.wait()


class Coordination(Protocol):
    """应用层使用的跨 Worker 协调边界。"""

    def get_cache(self, provider: str, key: str) -> Any | None:
        """读取 Provider 缓存；故障按未命中处理。"""

    def set_cache(self, provider: str, key: str, value: Any, ttl_seconds: int) -> None:
        """写入 Provider 缓存；故障不阻断主流程。"""

    def get_visitor(self, token_hash: str) -> UUID | None:
        """读取匿名访客缓存。"""

    def set_visitor(self, token_hash: str, visitor_id: UUID) -> None:
        """缓存匿名访客编号。"""

    def get_idempotency(self, visitor_id: UUID, key: str) -> UUID | None:
        """读取规划幂等提示。"""

    def set_idempotency(self, visitor_id: UUID, key: str, session_id: UUID) -> None:
        """缓存规划幂等结果。"""

    def session_lock(self, session_id: UUID) -> Any:
        """返回会话写锁异步上下文。"""

    def provider_slot(self, provider: str) -> Any:
        """返回 Provider 全局并发槽异步上下文。"""

    def request_lock(self, namespace: str, key: str) -> Any:
        """返回相同外部请求的跨 Worker 合并锁。"""

    def sync_provider_slot(self, provider: str) -> Any:
        """返回同步模型调用使用的全局并发槽。"""

    def task_lease(self, session_id: UUID) -> Any:
        """返回后台任务租约异步上下文。"""

    async def allow_request(self, identity: str) -> bool:
        """执行 API 固定窗口限流；Redis 故障时放行。"""


class LocalCoordination:
    """单进程测试使用的最小协调器，不用于生产多 Worker。"""

    def __init__(self) -> None:
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._request_locks: dict[str, asyncio.Lock] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._leases: set[UUID] = set()

    def get_cache(self, provider: str, key: str) -> Any | None:
        """单进程测试默认不保存 Provider 缓存。"""

        return None

    def set_cache(self, provider: str, key: str, value: Any, ttl_seconds: int) -> None:
        """单进程测试忽略 Provider 缓存写入。"""

        return None

    def get_visitor(self, token_hash: str) -> UUID | None:
        """单进程测试不缓存匿名访客。"""

        return None

    def set_visitor(self, token_hash: str, visitor_id: UUID) -> None:
        """单进程测试忽略匿名访客缓存写入。"""

        return None

    def get_idempotency(self, visitor_id: UUID, key: str) -> UUID | None:
        """单进程测试把幂等最终保障交给 Repository。"""

        return None

    def set_idempotency(self, visitor_id: UUID, key: str, session_id: UUID) -> None:
        """单进程测试忽略幂等提示缓存写入。"""

        return None

    @asynccontextmanager
    async def session_lock(self, session_id: UUID) -> AsyncIterator[None]:
        """使用进程内锁串行修改同一会话。"""

        async with self._locks.setdefault(session_id, asyncio.Lock()):
            yield

    @asynccontextmanager
    async def provider_slot(self, provider: str) -> AsyncIterator[None]:
        """使用宽松信号量模拟 Provider 并发槽。"""

        async with self._semaphores.setdefault(provider, asyncio.Semaphore(32)):
            yield

    @asynccontextmanager
    async def request_lock(self, namespace: str, key: str) -> AsyncIterator[None]:
        """在单进程内合并相同外部请求。"""

        async with self._request_locks.setdefault(f"{namespace}:{key}", asyncio.Lock()):
            yield

    @contextmanager
    def sync_provider_slot(self, provider: str) -> Iterator[None]:
        """同步测试调用不额外限制 Provider 并发。"""

        yield

    @asynccontextmanager
    async def task_lease(self, session_id: UUID) -> AsyncIterator[LeaseHandle | None]:
        """保证单进程内同一后台任务只有一个执行者。"""

        if session_id in self._leases:
            yield None
            return
        self._leases.add(session_id)
        handle = LeaseHandle("local", asyncio.Event())
        try:
            yield handle
        finally:
            self._leases.discard(session_id)

    async def allow_request(self, identity: str) -> bool:
        """单进程测试默认放行 API 请求。"""

        return True


class RedisCoordination:
    """使用 Redis 提供跨 Uvicorn Worker 的共享协调。"""

    def __init__(
        self,
        url: str,
        provider_limits: dict[str, int],
        *,
        timeout_seconds: float = 2,
        session_lock_ttl_seconds: int = 30,
        task_lease_ttl_seconds: int = 30,
        task_lease_renew_seconds: int = 10,
        api_rate_limit_per_minute: int = 120,
    ) -> None:
        self.url = url
        self.provider_limits = {name: max(1, value) for name, value in provider_limits.items()}
        self.session_lock_ttl_ms = max(5, session_lock_ttl_seconds) * 1000
        self.task_lease_ttl_ms = max(5, task_lease_ttl_seconds) * 1000
        self.renew_seconds = max(1, task_lease_renew_seconds)
        self.api_rate_limit = max(1, api_rate_limit_per_minute)
        self._local_session_locks: weakref.WeakValueDictionary[UUID, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        options: dict[str, Any] = {
            "socket_timeout": timeout_seconds,
            "decode_responses": True,
        }
        self._sync = Redis.from_url(url, **options) if url else None
        self._async = AsyncRedis.from_url(url, **options) if url else None

    def readiness(self) -> str:
        """检查 Redis 是否可响应轻量 PING。"""

        if self._sync is None:
            return "missing"
        try:
            return "ready" if self._sync.ping() else "unavailable"
        except RedisError:
            return "unavailable"

    def close(self) -> None:
        """关闭同步 Redis 连接池。"""

        if self._sync is not None:
            self._sync.close()

    async def aclose(self) -> None:
        """关闭异步 Redis 连接池。"""

        if self._async is not None:
            await self._async.aclose()

    def get_cache(self, provider: str, key: str) -> Any | None:
        """缓存不可用时回源 Provider，不把性能故障升级为业务故障。"""

        raw = self._safe_get(_provider_cache_key(provider, key), "provider_cache_get")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def set_cache(self, provider: str, key: str, value: Any, ttl_seconds: int) -> None:
        """写缓存失败只记录告警，事实结果仍可继续返回。"""

        if self._sync is None:
            return
        try:
            payload = _cache_json(value)
            self._sync.setex(_provider_cache_key(provider, key), max(1, ttl_seconds), payload)
        except (RedisError, TypeError, ValueError):
            LOGGER.warning("redis_cache_write_failed provider=%s", provider)

    def set_cache_strict(
        self, provider: str, key: str, value: Any, ttl_seconds: int
    ) -> None:
        """迁移脚本使用的严格写入，失败时必须阻止旧表被删除。"""

        client = self._require_sync()
        try:
            payload = _cache_json(value)
            client.setex(_provider_cache_key(provider, key), max(1, ttl_seconds), payload)
        except (RedisError, TypeError, ValueError) as error:
            raise CoordinationUnavailableError("Provider 缓存迁移写入失败") from error

    def get_visitor(self, token_hash: str) -> UUID | None:
        """访客缓存故障时由身份服务回退 PostgreSQL。"""

        value = self._safe_get(_visitor_key(token_hash), "visitor_cache_get")
        try:
            return UUID(value) if value else None
        except ValueError:
            return None

    def set_visitor(self, token_hash: str, visitor_id: UUID) -> None:
        """短期缓存匿名访客编号，数据库仍保存权威身份。"""

        self._safe_set(_visitor_key(token_hash), str(visitor_id), 300, "visitor_cache_set")

    def get_idempotency(self, visitor_id: UUID, key: str) -> UUID | None:
        """Redis 只做快速提示，PostgreSQL 唯一约束仍是最终保障。"""

        value = self._safe_get(_idempotency_key(visitor_id, key), "idempotency_get")
        try:
            return UUID(value) if value else None
        except ValueError:
            return None

    def set_idempotency(self, visitor_id: UUID, key: str, session_id: UUID) -> None:
        """缓存幂等结果用于快速命中，不能替代数据库唯一约束。"""

        self._safe_set(
            _idempotency_key(visitor_id, key), str(session_id), 86_400, "idempotency_set"
        )

    @asynccontextmanager
    async def session_lock(self, session_id: UUID) -> AsyncIterator[None]:
        """锁设置 TTL 并校验随机 Token 后释放，避免误删其他 Worker 的新锁。"""

        local_lock = self._local_session_locks.setdefault(session_id, asyncio.Lock())
        # 一个工作流会并行回报多个步骤。先在进程内排队，避免这些协程同时启动 Redis
        # 超时计时；Redis 锁仍负责不同 Worker 之间的最终互斥。
        async with local_lock:
            key = f"travel:session:{session_id}:lock"
            token = secrets.token_urlsafe(24)
            await self._acquire_lock(key, token, self.session_lock_ttl_ms, wait_seconds=10)
            stop = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._renew_string_lock(key, token, self.session_lock_ttl_ms, stop)
            )
            try:
                yield
            finally:
                stop.set()
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                await self._release_string_lock(key, token)

    @asynccontextmanager
    async def provider_slot(self, provider: str) -> AsyncIterator[None]:
        """所有 Worker 共享同一 Provider 并发计数。"""

        token = secrets.token_urlsafe(18)
        key = f"travel:provider:{provider}:slots"
        await self._acquire_slot(key, token, self._provider_limit(provider), wait_seconds=3)
        try:
            yield
        finally:
            await self._release_slot(key, token)

    @asynccontextmanager
    async def request_lock(self, namespace: str, key: str) -> AsyncIterator[None]:
        """同一缓存键只允许一个 Worker 回源，等待者取得锁后会再次检查缓存。"""

        # 冷缓存并发命中时只允许一个 Worker 访问上游，避免同一请求同时消耗供应商配额。
        lock_key = f"travel:request:{namespace}:{_digest(key)}:lock"
        token = secrets.token_urlsafe(24)
        await self._acquire_lock(
            lock_key,
            token,
            60_000,
            wait_seconds=30,
            busy_error=RateLimitExceededError(),
        )
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._renew_string_lock(lock_key, token, 60_000, stop)
        )
        try:
            yield
        finally:
            stop.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self._release_string_lock(lock_key, token)

    @contextmanager
    def sync_provider_slot(self, provider: str) -> Iterator[None]:
        """同步 LLM 客户端在线程中执行时使用相同的 Redis 全局槽。"""

        token = secrets.token_urlsafe(18)
        key = f"travel:provider:{provider}:slots"
        self._acquire_slot_sync(key, token, self._provider_limit(provider), wait_seconds=3)
        try:
            yield
        finally:
            self._release_slot_sync(key, token)

    @asynccontextmanager
    async def task_lease(self, session_id: UUID) -> AsyncIterator[LeaseHandle | None]:
        """租约确保同一后台会话在多个 Worker 中最多由一个执行。"""

        # PostgreSQL 保存任务事实，Redis 租约只负责当前执行权；租约过期后其他 Worker 才能接管。
        client = self._require_async()
        key = f"travel:task:{session_id}:lease"
        token = secrets.token_urlsafe(24)
        try:
            acquired = await client.set(key, token, nx=True, px=self.task_lease_ttl_ms)
        except RedisError as error:
            raise CoordinationUnavailableError("任务租约服务不可用") from error
        if not acquired:
            yield None
            return
        lost = asyncio.Event()
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._renew_string_lock(key, token, self.task_lease_ttl_ms, stop, lost)
        )
        try:
            yield LeaseHandle(token, lost)
        finally:
            stop.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self._release_string_lock(key, token)

    async def allow_request(self, identity: str) -> bool:
        """API 限流故障时 fail-open，并记录基础设施告警。"""

        if self._async is None:
            return True
        key = _rate_limit_key(identity)
        try:
            count = await self._async.incr(key)
            if count == 1:
                await self._async.expire(key, 60)
            return int(count) <= self.api_rate_limit
        except (RedisError, RuntimeError, AttributeError):
            # API 限流属于保护性能力。热重载和测试会切换事件循环，旧异步连接也应按
            # Redis 不可用处理，不能让限流设施反过来中断正常业务请求。
            LOGGER.warning("redis_rate_limit_failed")
            return True

    def _safe_get(self, key: str, event: str) -> str | None:
        if self._sync is None:
            return None
        try:
            value = self._sync.get(key)
            return str(value) if value is not None else None
        except RedisError:
            LOGGER.warning("%s", event)
            return None

    def _safe_set(self, key: str, value: str, ttl: int, event: str) -> None:
        if self._sync is None:
            return
        try:
            self._sync.setex(key, ttl, value)
        except RedisError:
            LOGGER.warning("%s", event)

    def _require_async(self) -> Any:
        if self._async is None:
            raise CoordinationUnavailableError("Redis 尚未配置")
        return self._async

    def _require_sync(self) -> Redis:
        if self._sync is None:
            raise CoordinationUnavailableError("Redis 尚未配置")
        return self._sync

    async def _acquire_lock(
        self,
        key: str,
        token: str,
        ttl_ms: int,
        wait_seconds: int,
        busy_error: Exception | None = None,
    ) -> None:
        client = self._require_async()
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            try:
                if await client.set(key, token, nx=True, px=ttl_ms):
                    return
            except RedisError as error:
                raise CoordinationUnavailableError("会话锁服务不可用") from error
            await asyncio.sleep(0.05)
        raise busy_error or SessionBusyError()

    async def _renew_string_lock(
        self,
        key: str,
        token: str,
        ttl_ms: int,
        stop: asyncio.Event,
        lost: asyncio.Event | None = None,
    ) -> None:
        client = self._require_async()
        while not stop.is_set():
            await asyncio.sleep(self.renew_seconds)
            try:
                renewed = await client.eval(_COMPARE_EXPIRE, 1, key, token, ttl_ms)
            except RedisError:
                renewed = 0
            if not renewed:
                if lost is not None:
                    lost.set()
                return

    async def _release_string_lock(self, key: str, token: str) -> None:
        client = self._require_async()
        try:
            await client.eval(_COMPARE_DELETE, 1, key, token)
        except RedisError:
            LOGGER.warning("redis_lock_release_failed key=%s", key)

    async def _acquire_slot(
        self, key: str, token: str, limit: int, wait_seconds: int
    ) -> None:
        client = self._require_async()
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            now_ms = int(time.time() * 1000)
            try:
                acquired = await client.eval(
                    _ACQUIRE_SLOT, 1, key, now_ms, limit, now_ms + 120_000, token, 120_000
                )
            except RedisError as error:
                raise CoordinationUnavailableError("Provider 并发协调不可用") from error
            if acquired:
                return
            await asyncio.sleep(0.05)
        raise RateLimitExceededError()

    async def _release_slot(self, key: str, token: str) -> None:
        client = self._require_async()
        try:
            await client.zrem(key, token)
        except RedisError:
            LOGGER.warning("redis_provider_slot_release_failed key=%s", key)

    def _acquire_slot_sync(
        self, key: str, token: str, limit: int, wait_seconds: int
    ) -> None:
        client = self._require_sync()
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            now_ms = int(time.time() * 1000)
            try:
                acquired = client.eval(
                    _ACQUIRE_SLOT, 1, key, now_ms, limit, now_ms + 120_000, token, 120_000
                )
            except RedisError as error:
                raise CoordinationUnavailableError("Provider 并发协调不可用") from error
            if acquired:
                return
            time.sleep(0.05)
        raise RateLimitExceededError()

    def _release_slot_sync(self, key: str, token: str) -> None:
        try:
            self._require_sync().zrem(key, token)
        except RedisError:
            LOGGER.warning("redis_provider_slot_release_failed key=%s", key)

    def _provider_limit(self, provider: str) -> int:
        return self.provider_limits.get(provider, 1)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cache_json(value: Any) -> str:
    """把领域模型转换为可稳定恢复的 JSON，不使用会丢失结构的字符串兜底。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"不支持写入 Redis 缓存的类型：{type(value).__name__}")


def _provider_cache_key(provider: str, key: str) -> str:
    return f"travel:provider:{provider}:cache:{_digest(key)}"


def _visitor_key(token_hash: str) -> str:
    return f"travel:visitor:{token_hash}"


def _idempotency_key(visitor_id: UUID, key: str) -> str:
    return f"travel:idempotency:{visitor_id}:{_digest(key)}"


def _rate_limit_key(identity: str) -> str:
    return f"travel:ratelimit:{_digest(identity)}"
