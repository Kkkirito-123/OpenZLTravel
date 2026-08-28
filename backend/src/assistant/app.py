"""独立 Assistant Service 的 FastAPI 与 SSE 边界。

该应用只提供健康检查和 ``POST /api/assistant/turn``。身份由匿名 Cookie 或签名身份
边界提供，会话状态由请求携带的 Assistant Session Token 恢复；服务端不建立对话数据库。
SSE 事件是前端唯一需要理解的输出协议，任何异常都转换为 ``error`` 后以 ``done`` 收尾。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from assistant.errors import AssistantModelError
from assistant.models import AssistantTurnRequest
from assistant.service import AssistantService
from runtime.config import Settings, get_settings
from runtime.container import get_assistant_dependencies
from runtime.identity import IdentityError, authenticate_identity
from runtime.tokens import SignedPayloadCodec, TokenError

logger = logging.getLogger(__name__)


def create_app(  # noqa: C901 - the standalone service intentionally has one route boundary
    service: AssistantService | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """创建只包含健康检查和一轮 Assistant SSE 的独立服务。"""

    resolved_settings = settings or get_settings()
    resolved_service = service or AssistantService(
        get_assistant_dependencies(resolved_settings),
        resolved_settings,
        SignedPayloadCodec(resolved_settings.signing_secret),
    )
    application = FastAPI(
        title="OpenZLTravel Assistant",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get("/ok")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/api/assistant/turn")
    async def assistant_turn(
        payload: AssistantTurnRequest,
        request: Request,
    ) -> StreamingResponse:
        try:
            identity = authenticate_identity(
                resolved_settings,
                path=request.url.path,
                headers=dict(request.headers),
                scope=request.scope,
            )
        except IdentityError as error:
            raise HTTPException(status_code=401, detail=error.code) from error

        async def stream() -> AsyncIterator[str]:
            try:
                events = await resolved_service.turn(payload, identity.user_id)
                for event, data in events:
                    yield _sse(event, data)
            except TokenError as error:
                yield _sse("error", {"code": error.code, "message": error.message})
                yield _sse("done", {})
            except ValueError as error:
                yield _sse("error", {"code": "invalid_assistant_input", "message": str(error)})
                yield _sse("done", {})
            except AssistantModelError as error:
                logger.exception("assistant model turn failed")
                yield _sse(
                    "error",
                    {"code": "assistant_model_error", "message": str(error)},
                )
                yield _sse("done", {})
            except Exception:
                logger.exception("assistant turn failed")
                yield _sse(
                    "error",
                    {"code": "assistant_unavailable", "message": "旅行助手暂时不可用。"},
                )
                yield _sse("done", {})

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return application


def _sse(event: str, data: object) -> str:
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {encoded}\n\n"


app = create_app()
