"""12306 等只读工具使用的最小 MCP Streamable HTTP 客户端。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, cast

import httpx

from .base import ProviderError


class McpHttpClient:
    """复用异步连接池的 MCP 客户端，只暴露无副作用的工具调用。"""

    protocol_version = "2025-06-18"

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float,
        bearer_token: str = "",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        # 无论连接池由谁创建，都保留这组协议头。这样测试注入的 httpx 客户端和
        # 生产环境的共享连接池都会携带 Accept 与 Bearer Token。
        self._base_headers = headers
        self.http = http or httpx.AsyncClient(timeout=timeout_seconds, headers=headers)
        self._owns_http = http is None
        self._request_id = 0
        self._session_id: str | None = None
        self._initialized = False
        self._legacy_direct = False
        self._initialize_lock = asyncio.Lock()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """调用一个 MCP 工具并提取结构化结果。"""

        await self._ensure_initialized()
        return await self._call_tool(name, arguments, allow_reinitialize=True)

    async def aclose(self) -> None:
        """尽力结束服务端会话，并关闭本实例创建的连接池。"""

        if self._session_id:
            with contextlib.suppress(httpx.HTTPError):
                await self.http.delete(self.url, headers=self._request_headers())
        if self._owns_http:
            await self.http.aclose()

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if not self._initialized:
                await self._initialize()

    async def _initialize(self) -> None:
        request_id = self._next_id()
        response = await self.http.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "travel-workbench", "version": "1.0.0"},
                },
                "id": request_id,
            },
            headers=self._request_headers(),
        )
        if response.status_code in {404, 405}:
            self._enable_legacy_direct()
            return
        response.raise_for_status()
        payload = _response_payload(response, request_id)
        if _method_not_found(payload):
            self._enable_legacy_direct()
            return
        if "error" in payload or not isinstance(payload.get("result"), dict):
            raise ProviderError("mcp_initialization_failed", "外部工具协议初始化失败")
        result = cast(dict[str, Any], payload["result"])
        negotiated = result.get("protocolVersion")
        if isinstance(negotiated, str) and negotiated:
            self.protocol_version = negotiated
        self._session_id = response.headers.get("Mcp-Session-Id")
        await self._send_initialized()
        self._initialized = True

    async def _send_initialized(self) -> None:
        response = await self.http.post(
            self.url,
            headers=self._request_headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        response.raise_for_status()

    async def _call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        allow_reinitialize: bool,
    ) -> Any:
        request_id = self._next_id()
        used_session = self._session_id
        response = await self.http.post(
            self.url,
            headers=self._request_headers(),
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
            raise ProviderError("mcp_tool_failed", "外部工具返回了业务错误")
        return _tool_result(payload.get("result"))

    async def _expire_session(self, expired: str) -> None:
        async with self._initialize_lock:
            if self._session_id != expired:
                return
            self._session_id = None
            self._initialized = False
            self._legacy_direct = False
            self.protocol_version = "2025-06-18"

    def _protocol_headers(self) -> dict[str, str]:
        if self._legacy_direct:
            return {}
        headers = {"MCP-Protocol-Version": self.protocol_version}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _request_headers(self) -> dict[str, str]:
        """合并认证、内容协商和当前 MCP 会话头。"""

        headers = dict(self._base_headers)
        headers.update(self._protocol_headers())
        return headers

    def _enable_legacy_direct(self) -> None:
        # 只在服务端明确不支持 initialize 时，才兼容早期直接调用模式。
        self._session_id = None
        self._legacy_direct = True
        self._initialized = True

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id


def _response_payload(response: httpx.Response, request_id: int) -> dict[str, Any]:
    if "text/event-stream" not in response.headers.get("content-type", ""):
        payload = response.json()
        if not isinstance(payload, dict):
            raise ProviderError("mcp_invalid_response", "MCP 返回了无法识别的数据")
        return cast(dict[str, Any], payload)
    payloads = [item for item in _sse_payloads(response.text) if item.get("id") == request_id]
    if not payloads:
        raise ProviderError("mcp_invalid_response", "MCP 返回了无法识别的事件")
    return payloads[-1]


def _sse_payloads(content: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for event in content.replace("\r\n", "\n").split("\n\n"):
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
        value = item.get("text", "")
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return result
