"""供应商共享的异步稳定性边界。

这里只保留本地开发阶段必需的能力：进程内 TTL 缓存、同请求合并、
每个 Provider 的并发上限、硬超时和最多一次网络重试。分布式缓存与锁不在
首版开发运行时中预埋。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

import httpx

T = TypeVar("T")


class ProviderError(RuntimeError):
    """可安全跨越图节点边界的供应商错误。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class CatalogUnavailableError(ProviderError):
    """地点目录基础设施不可用，不允许放大为批量高德请求。"""

    def __init__(self, message: str = "PostgreSQL 地点库暂时不可用") -> None:
        super().__init__("catalog_unavailable", message)


@dataclass(frozen=True)
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class AsyncTTLCache:
    """小型进程内 TTL 缓存，只用于本地 Agent Server。"""

    def __init__(self, *, max_entries: int = 256) -> None:
        self.max_entries = max(1, max_entries)
        self._values: dict[str, _CacheEntry[Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        """返回未过期值；过期项在读取时就地清理。"""

        async with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                self._values.pop(key, None)
                return None
            return entry.value

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        """保存带过期时间的值，超限时先清理最早过期项。"""

        if ttl_seconds <= 0:
            return
        async with self._lock:
            if len(self._values) >= self.max_entries and key not in self._values:
                oldest = min(self._values, key=lambda item: self._values[item].expires_at)
                self._values.pop(oldest, None)
            self._values[key] = _CacheEntry(value, time.monotonic() + ttl_seconds)

    async def clear(self) -> None:
        """清空本地缓存，主要供测试和开发热重载使用。"""

        async with self._lock:
            self._values.clear()


class ProviderRuntime:
    """统一执行一个 Provider 的缓存、限流、超时和网络重试。"""

    def __init__(
        self,
        name: str,
        *,
        timeout_seconds: float,
        concurrency: int = 3,
        network_retries: int = 1,
        cache: AsyncTTLCache | None = None,
    ) -> None:
        self.name = name
        self.timeout_seconds = max(0.01, timeout_seconds)
        # 开发版严格限制为最多一次重试，防止配置错误放大上游压力。
        self.network_retries = min(1, max(0, network_retries))
        self.cache = cache or AsyncTTLCache()
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._inflight: dict[str, asyncio.Task[tuple[Any, bool]]] = {}
        self._inflight_lock = asyncio.Lock()

    async def run(
        self,
        key: str,
        operation: Callable[[], Awaitable[T]],
        *,
        ttl_seconds: float,
    ) -> tuple[T, bool]:
        """执行一次可缓存请求，并返回 ``(结果, 是否命中缓存)``。"""

        cached = await self.cache.get(key)
        if cached is not None:
            return cast(T, cached), True
        async with self._inflight_lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._execute(key, operation, ttl_seconds))
                self._inflight[key] = task
                task.add_done_callback(lambda completed: self._discard(key, completed))
        # 取消一个图 Run 不应中断其他 Run 正在共享的同 key 请求。
        result, cache_hit = await asyncio.shield(task)
        return cast(T, result), cache_hit

    def _discard(self, key: str, completed: asyncio.Future[Any]) -> None:
        if self._inflight.get(key) is completed:
            self._inflight.pop(key, None)
        if not completed.cancelled():
            with contextlib.suppress(Exception):
                completed.exception()

    async def _execute(
        self,
        key: str,
        operation: Callable[[], Awaitable[T]],
        ttl_seconds: float,
    ) -> tuple[T, bool]:
        async with self._semaphore:
            for attempt in range(self.network_retries + 1):
                try:
                    async with asyncio.timeout(self.timeout_seconds):
                        result = await operation()
                    await self.cache.set(key, result, ttl_seconds)
                    return result, False
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if attempt < self.network_retries and _retryable(error):
                        continue
                    raise _stable_error(self.name, error) from error
        raise ProviderError(f"{self.name}_unavailable", "外部数据源暂时不可用")


def stable_key(*values: Any) -> str:
    """生成不包含密钥、Token 或 Cookie 的稳定缓存键。"""

    return json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)


def stable_fact_id(namespace: str, *parts: Any) -> str:
    """为外部事实生成跨 Run 稳定 ID，供 Agent 只读引用。"""

    raw = stable_key(namespace, *parts).encode("utf-8")
    return f"{namespace}:{hashlib.sha256(raw).hexdigest()[:20]}"


def _retryable(error: BaseException) -> bool:
    if isinstance(error, ProviderError):
        return error.retryable
    if isinstance(error, (TimeoutError, httpx.TimeoutException, httpx.NetworkError)):
        return True
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code >= 500


def _stable_error(provider: str, error: BaseException) -> ProviderError:
    if isinstance(error, ProviderError):
        return error
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return ProviderError(f"{provider}_timeout", "外部数据源请求超时")
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in {401, 403}:
            return ProviderError(f"{provider}_unauthorized", "外部数据源认证失败")
        if status == 429:
            return ProviderError(f"{provider}_rate_limited", "外部数据源请求过于频繁")
    if isinstance(error, (httpx.HTTPError, OSError)):
        return ProviderError(f"{provider}_unavailable", "外部数据源连接失败")
    return ProviderError(f"{provider}_invalid_response", "外部数据源返回了无法识别的数据")
