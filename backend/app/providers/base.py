"""外部 Provider 的共享稳定性设施。

本文件集中 MCP Streamable HTTP、SQLite 缓存、同请求合并和熔断。具体供应商只负责
构造工具参数和解析领域模型，不得各自实现一套重试循环。
"""

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar, cast

import httpx

from app.errors import ProviderError

T = TypeVar("T")


class CacheStore(Protocol):
    """Provider 只依赖的最小缓存接口。"""

    def get_cache(self, provider: str, key: str) -> Any | None:
        """读取未过期缓存。"""

        ...

    def set_cache(self, provider: str, key: str, value: Any, ttl_seconds: int) -> None:
        """写入带 TTL 的缓存。"""

        ...


class CircuitBreaker:
    """连续网络失败后短暂拒绝请求，保护本地任务不被上游拖垮。"""

    def __init__(self, threshold: int = 3, cooldown_seconds: float = 30) -> None:
        self.threshold = max(1, threshold)
        self.cooldown_seconds = cooldown_seconds
        self.failures = 0
        self.opened_until = 0.0

    def ensure_available(self) -> None:
        """熔断期间快速失败，不继续堆积外部请求。"""

        if time.monotonic() < self.opened_until:
            raise ProviderError("provider_circuit_open", "外部数据源暂时不可用，请稍后重试")

    def success(self) -> None:
        """成功后恢复闭合状态。"""

        self.failures = 0
        self.opened_until = 0.0

    def failure(self) -> None:
        """记录网络失败，达到阈值后进入冷却。"""

        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_until = time.monotonic() + self.cooldown_seconds


class ProviderExecutor:
    """为 Provider 提供缓存、去重、限并发和一次网络重试。"""

    def __init__(
        self,
        provider: str,
        cache: CacheStore,
        concurrency: int = 4,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.breaker = CircuitBreaker(failure_threshold, cooldown_seconds)
        self.inflight: dict[str, asyncio.Task[Any]] = {}
        self.lock = asyncio.Lock()

    async def run(
        self,
        key: str,
        ttl_seconds: int,
        operation: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        """命中缓存直接返回；同一进程的相同查询共享一个任务。"""

        cached = self.cache.get_cache(self.provider, key)
        if cached is not None:
            return cast(T, cached), True
        async with self.lock:
            task = self.inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._execute(key, ttl_seconds, operation))
                self.inflight[key] = task
                task.add_done_callback(lambda completed: self._discard_inflight(key, completed))
        # 单个 HTTP 请求取消不应中断同 key 的共享查询；结果仍会写入缓存，
        # 后续等待者或下一次查询可以继续复用它。
        return cast(tuple[T, bool], await asyncio.shield(task))

    def _discard_inflight(self, key: str, completed: asyncio.Future[Any]) -> None:
        """仅由完成任务清理自身，避免取消的等待者提前移除在途请求。"""

        if self.inflight.get(key) is completed:
            self.inflight.pop(key, None)
        # 所有等待者都取消时也要读取异常，避免 asyncio 在日志中报未处理异常。
        if not completed.cancelled():
            with contextlib.suppress(Exception):
                completed.exception()

    async def _execute(
        self, key: str, ttl_seconds: int, operation: Callable[[], Awaitable[T]]
    ) -> tuple[T, bool]:
        self.breaker.ensure_available()
        async with self.semaphore:
            for attempt in range(2):
                try:
                    result = await operation()
                    self.breaker.success()
                    self.cache.set_cache(self.provider, key, result, ttl_seconds)
                    return result, False
                except httpx.HTTPStatusError as error:
                    status = error.response.status_code
                    if status >= 500 and attempt == 0:
                        self.breaker.failure()
                        continue
                    raise _http_provider_error(self.provider, status) from error
                except (httpx.TimeoutException, httpx.NetworkError) as error:
                    self.breaker.failure()
                    if attempt == 1:
                        raise ProviderError(
                            f"{self.provider}_unavailable", "外部数据源连接失败"
                        ) from error
        raise ProviderError(f"{self.provider}_unavailable", "外部数据源暂时不可用")


class McpHttpClient:
    """只读 MCP Streamable HTTP 客户端，兼容有状态与旧式无状态端点。"""

    PROTOCOL_VERSION = "2025-06-18"

    def __init__(self, url: str, timeout_seconds: float, bearer_token: str = "") -> None:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        self.http = httpx.AsyncClient(timeout=timeout_seconds, headers=headers)
        self.url = url
        self.request_id = 0
        self.protocol_version = self.PROTOCOL_VERSION
        self.session_id: str | None = None
        self.initialized = False
        self.legacy_direct = False
        self.initialize_lock = asyncio.Lock()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """调用一个只读工具并提取其第一个结构化或文本结果。"""

        await self._ensure_initialized()
        return await self._call_tool(name, arguments, allow_reinitialize=True)

    async def aclose(self) -> None:
        """尽力结束有状态会话并关闭连接池，关闭失败不影响应用退出。"""

        if self.session_id:
            with contextlib.suppress(httpx.HTTPError):
                await self.http.delete(self.url, headers=self._protocol_headers())
        await self.http.aclose()

    async def _ensure_initialized(self) -> None:
        if self.initialized:
            return
        async with self.initialize_lock:
            if self.initialized:
                return
            await self._initialize()

    async def _initialize(self) -> None:
        request_id = self._next_request_id()
        response = await self.http.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": self.PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "OpenZLTravel", "version": "0.3.0"},
                },
                "id": request_id,
            },
        )
        if response.status_code in {404, 405}:
            self._enable_legacy_direct()
            return
        response.raise_for_status()
        payload = _response_payload(response, request_id)
        if _method_not_found(payload):
            self._enable_legacy_direct()
            return
        if "error" in payload:
            raise ProviderError("mcp_initialization_failed", "外部工具协议初始化失败")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ProviderError("mcp_invalid_response", "外部工具返回了无法识别的初始化响应")
        negotiated = result.get("protocolVersion")
        if isinstance(negotiated, str) and negotiated:
            self.protocol_version = negotiated
        self.session_id = response.headers.get("Mcp-Session-Id")
        await self._send_initialized_notification()
        self.initialized = True

    async def _send_initialized_notification(self) -> None:
        response = await self.http.post(
            self.url,
            headers=self._protocol_headers(),
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        response.raise_for_status()

    async def _call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        allow_reinitialize: bool,
    ) -> Any:
        request_id = self._next_request_id()
        used_session = self.session_id
        response = await self.http.post(
            self.url,
            headers=self._protocol_headers(),
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
                "id": request_id,
            },
        )
        if response.status_code == 404 and used_session and allow_reinitialize:
            await self._expire_session(used_session)
            await self._ensure_initialized()
            return await self._call_tool(name, arguments, allow_reinitialize=False)
        response.raise_for_status()
        payload = _response_payload(response, request_id)
        if "error" in payload:
            raise ProviderError("mcp_tool_failed", "外部工具返回业务错误")
        return _tool_result(payload.get("result", {}))

    async def _expire_session(self, expired_session: str) -> None:
        async with self.initialize_lock:
            if self.session_id != expired_session:
                return
            self.session_id = None
            self.initialized = False
            self.legacy_direct = False
            self.protocol_version = self.PROTOCOL_VERSION

    def _protocol_headers(self) -> dict[str, str]:
        if self.legacy_direct:
            return {}
        headers = {"MCP-Protocol-Version": self.protocol_version}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _enable_legacy_direct(self) -> None:
        # 少数早期兼容服务允许直接 tools/call；只在明确不支持 initialize 时降级。
        self.session_id = None
        self.legacy_direct = True
        self.initialized = True

    def _next_request_id(self) -> int:
        self.request_id += 1
        return self.request_id


def _response_payload(response: httpx.Response, request_id: int | None = None) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        return cast(dict[str, Any], response.json())
    payloads = _sse_payloads(response.text)
    if request_id is not None:
        payloads = [item for item in payloads if item.get("id") == request_id]
    if not payloads:
        raise ProviderError("mcp_invalid_response", "外部工具返回了无法识别的响应")
    return payloads[-1]


def _sse_payloads(content: str) -> list[dict[str, Any]]:
    """提取 SSE 中的 JSON-RPC 数据，忽略心跳和非 JSON 事件。"""

    payloads: list[dict[str, Any]] = []
    normalized = content.replace("\r\n", "\n")
    for event in normalized.split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
        )
        if not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _method_not_found(payload: dict[str, Any]) -> bool:
    error = payload.get("error")
    return isinstance(error, dict) and error.get("code") == -32601


def _tool_result(result: Any) -> Any:
    if isinstance(result, dict) and result.get("structuredContent") is not None:
        return result["structuredContent"]
    content = result.get("content", []) if isinstance(result, dict) else []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text", "")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
    return result


def stable_key(*values: Any) -> str:
    """生成不包含密钥的稳定缓存键。"""

    return json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)


def _http_provider_error(provider: str, status: int) -> ProviderError:
    """认证、限流和业务错误不重试，并转换为安全稳定错误。"""

    if status in {401, 403}:
        return ProviderError(f"{provider}_unauthorized", "外部数据源认证失败")
    if status == 429:
        return ProviderError(f"{provider}_rate_limited", "外部数据源请求过于频繁")
    return ProviderError(f"{provider}_unavailable", "外部数据源暂时不可用")
