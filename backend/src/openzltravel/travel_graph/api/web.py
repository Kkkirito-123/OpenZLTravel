"""挂载到 LangGraph Agent Server 的最小 FastAPI 自定义应用。

平台自身负责 Thread、Run、Checkpoint 和 Store；本模块只补充匿名身份、历史行程读取
和删除接口。它不接受旅行需求、不创建规划状态，也不替代 Graph 的工单验证节点。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from langgraph.config import get_store
from langgraph.store.base import BaseStore
from pydantic import BaseModel, ConfigDict

from openzltravel.domain.models import TripRecord
from openzltravel.runtime.config import Settings, get_settings
from openzltravel.runtime.identity import (
    IdentityCodec,
    IdentityError,
    authenticate_identity,
    cookie_from_headers,
)
from openzltravel.travel_graph.api.trips import TripNotFoundError, TripStoreService, TripSummary


class AnonymousAuthResponse(BaseModel):
    """匿名身份初始化响应；签名 Token 只写 HttpOnly Cookie，不进入 JSON。"""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    expires_at: str


def create_app() -> FastAPI:  # noqa: C901 - routes intentionally share one auth boundary
    """创建只包含身份和行程历史接口的自定义应用。

    LangGraph Agent Server 已提供 Thread、Run、Checkpoint 和 Store 的平台接口，因此这里
    关闭 FastAPI 自带的文档路由，避免自定义应用看起来又形成一套独立业务 API。
    """

    application = FastAPI(
        title="OpenZLTravel API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    @application.post("/api/auth/anonymous", response_model=AnonymousAuthResponse)
    async def issue_anonymous_identity(
        request: Request,
        response: Response,
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> AnonymousAuthResponse:
        """创建或续签匿名身份，并设置 HMAC、HttpOnly、SameSite=Lax Cookie。"""

        codec = IdentityCodec(settings.signing_secret, settings.cookie_ttl_seconds)
        current = (
            None
            if settings.auth_mode == "dev"
            else _optional_cookie_identity(request, settings, codec)
        )
        user_id = settings.dev_user_id if settings.auth_mode == "dev" else None
        token, identity = codec.issue(current or user_id)
        response.set_cookie(
            key=settings.cookie_name,
            value=token,
            max_age=settings.cookie_ttl_seconds,
            expires=identity.expires_at,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return AnonymousAuthResponse(
            user_id=identity.user_id,
            expires_at=identity.expires_at.isoformat(),
        )

    @application.get("/api/trips", response_model=list[TripSummary])
    async def list_trips(
        request: Request,
        store: Annotated[BaseStore, Depends(_get_store)],
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> list[TripSummary]:
        """返回当前认证身份的最终行程历史。"""

        user_id = _request_user_id(request, settings)
        return await TripStoreService(store).list(user_id)

    @application.get("/api/trips/{trip_id}", response_model=TripRecord)
    async def get_trip(
        trip_id: UUID,
        request: Request,
        store: Annotated[BaseStore, Depends(_get_store)],
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> TripRecord:
        """返回当前认证身份拥有的一条完整行程。"""

        user_id = _request_user_id(request, settings)
        try:
            return await TripStoreService(store).get(user_id, trip_id)
        except TripNotFoundError as error:
            raise HTTPException(status_code=404, detail="trip_not_found") from error

    @application.delete("/api/trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_trip(
        trip_id: UUID,
        request: Request,
        store: Annotated[BaseStore, Depends(_get_store)],
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> Response:
        """删除当前认证身份拥有的一条行程。"""

        user_id = _request_user_id(request, settings)
        try:
            await TripStoreService(store).delete(user_id, trip_id)
        except TripNotFoundError as error:
            raise HTTPException(status_code=404, detail="trip_not_found") from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return application


def _get_store() -> BaseStore:
    """取得 Agent Server 注入的 Store；独立 Web 测试可覆盖此依赖。"""

    return get_store()


def _request_user_id(request: Request, settings: Settings) -> str:
    """优先使用 Agent Server 已认证身份，并为独立 HTTP 测试保留同规则校验。

    自定义应用挂载到 Agent Server 时，认证层会把用户放入 ASGI scope；直接创建 FastAPI
    TestClient 时没有该上下文，才回退到同一套 Cookie/loopback 解析。两条路径最终都拒绝
    ``anonymous-bootstrap``，因为引导身份只允许签发 Cookie，不能读取行程。
    """

    user = request.scope.get("user")
    identity = getattr(user, "identity", None)
    if isinstance(identity, str) and identity != "anonymous-bootstrap":
        return identity
    try:
        resolved = authenticate_identity(
            settings,
            path=request.url.path,
            headers=dict(request.headers),
            scope=request.scope,
        )
    except IdentityError as error:
        raise HTTPException(status_code=401, detail=error.code) from error
    if resolved.user_id == "anonymous-bootstrap":
        raise HTTPException(status_code=401, detail="auth_cookie_missing")
    return resolved.user_id


def _optional_cookie_identity(
    request: Request,
    settings: Settings,
    codec: IdentityCodec,
) -> str | None:
    """在续签前验证现有 Cookie，绝不把无效身份静默替换成新用户。

    若篡改或过期 Cookie 被当作“未登录”直接续签，客户端会在不知情时丢失原命名空间，
    同时也会让攻击请求绕过应有的 401。这里因此只允许真正缺失 Cookie 时创建新身份。
    """

    token = cookie_from_headers(dict(request.headers), settings.cookie_name)
    if token is None:
        return None
    try:
        return codec.verify(token).user_id
    except IdentityError as error:
        # 篡改或过期 Cookie 必须显式失败，不能静默换发身份导致越权边界模糊。
        raise HTTPException(status_code=401, detail=error.code) from error


app = create_app()
