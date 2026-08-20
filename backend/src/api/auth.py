"""LangGraph Agent Server 的认证与所有权授权规则。

认证只回答“当前请求是谁”，授权再把 Thread、Run 和 Store 限定到该身份。
所有未显式开放的内置资源默认拒绝，避免 LangGraph 新增端点后意外跨用户开放。
"""

from __future__ import annotations

from typing import Any

from langgraph_sdk import Auth
from langgraph_sdk.auth.types import (
    AssistantsRead,
    AssistantsSearch,
    RunsCreate,
    ThreadsCreate,
    ThreadsDelete,
    ThreadsRead,
    ThreadsSearch,
    ThreadsUpdate,
)

from api.identity import IdentityError, authenticate_identity
from runtime.config import get_settings

auth = Auth()


@auth.authenticate
async def authenticate(
    path: str,
    headers: dict[bytes, bytes] | None,
    scope: dict[str, Any],
) -> Auth.types.MinimalUserDict:
    """验证开发本机身份或 HMAC Cookie，并写入统一认证上下文。"""

    try:
        identity = authenticate_identity(
            get_settings(), path=path, headers=headers, scope=scope
        )
    except IdentityError as error:
        raise Auth.exceptions.HTTPException(status_code=401, detail=error.code) from error
    return {
        "identity": identity.user_id,
        "permissions": ["travel"],
    }


@auth.on
async def deny_unhandled(
    ctx: Auth.types.AuthContext, value: Auth.types.on.value
) -> bool:
    """拒绝所有未显式列出的 Agent Server 资源操作。"""

    del ctx, value
    return False


@auth.on.assistants.read
async def allow_travel_assistant_read(
    ctx: Auth.types.AuthContext,
    value: AssistantsRead,
) -> bool:
    """允许已认证用户读取唯一的 travel Assistant。"""

    del ctx, value
    return True


@auth.on.assistants.search
async def allow_travel_assistant_search(
    ctx: Auth.types.AuthContext,
    value: AssistantsSearch,
) -> bool:
    """允许 SDK 查找 travel Assistant；创建和修改仍由默认拒绝规则阻止。"""

    del ctx
    graph_id = value.get("graph_id")
    return graph_id in {None, "travel"}


@auth.on.threads.create
async def own_created_thread(
    ctx: Auth.types.AuthContext,
    value: ThreadsCreate,
) -> None:
    """创建 Thread 时覆盖客户端 owner，防止伪造其他用户身份。"""

    value.setdefault("metadata", {})["owner"] = ctx.user.identity


@auth.on.threads.read
async def filter_thread_read(
    ctx: Auth.types.AuthContext,
    value: ThreadsRead,
) -> Auth.types.FilterType:
    """读取 Thread 或 Run 时强制匹配当前 owner。"""

    del value
    return _owner_filter(ctx)


@auth.on.threads.search
async def filter_thread_search(
    ctx: Auth.types.AuthContext,
    value: ThreadsSearch,
) -> Auth.types.FilterType:
    """搜索 Thread 或 Run 时只返回当前用户的数据。"""

    del value
    return _owner_filter(ctx)


@auth.on.threads.update
async def filter_thread_update(
    ctx: Auth.types.AuthContext,
    value: ThreadsUpdate,
) -> Auth.types.FilterType:
    """更新、回滚或中断 Thread 时校验 owner。"""

    metadata = value.get("metadata")
    if metadata is not None:
        metadata["owner"] = ctx.user.identity
    return _owner_filter(ctx)


@auth.on.threads.delete
async def filter_thread_delete(
    ctx: Auth.types.AuthContext,
    value: ThreadsDelete,
) -> Auth.types.FilterType:
    """删除 Thread 或 Run 时校验 owner。"""

    del value
    return _owner_filter(ctx)


@auth.on.threads.create_run
async def filter_run_create(
    ctx: Auth.types.AuthContext,
    value: RunsCreate,
) -> Auth.types.FilterType:
    """启动或恢复 Run 时既写入 Run owner，也验证所属 Thread owner。"""

    value.setdefault("metadata", {})["owner"] = ctx.user.identity
    return _owner_filter(ctx)


@auth.on.store
async def scope_store(
    ctx: Auth.types.AuthContext,
    value: dict[str, Any],
) -> bool:
    """把所有 Store 操作收敛到 ``(user_id, trips|preferences)`` 命名空间。"""

    namespace = tuple(value.get("namespace") or ())
    if namespace and namespace[0] == ctx.user.identity:
        scoped = namespace
    else:
        scoped = (ctx.user.identity, *namespace)
    if len(scoped) < 2 or scoped[1] not in {"trips", "preferences"}:
        return False
    value["namespace"] = scoped
    return True


def _owner_filter(ctx: Auth.types.AuthContext) -> Auth.types.FilterType:
    """构造 Agent Server 可识别的元数据所有权过滤器。"""

    return {"owner": ctx.user.identity}
