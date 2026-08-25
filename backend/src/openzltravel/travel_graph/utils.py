"""Graph 节点共享的小型、无业务分支工具。"""

from __future__ import annotations

from collections.abc import Mapping

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from openzltravel.travel_graph.state import GraphNotice, TravelContext


def user_id_from(config: RunnableConfig, runtime: Runtime[TravelContext]) -> str:
    """认证用户 ID 优先，离线测试和本地开发依次降级。"""

    configurable = config.get("configurable", {})
    if isinstance(configurable, Mapping):
        authenticated = configurable.get("langgraph_auth_user_id")
        if isinstance(authenticated, str) and authenticated:
            return authenticated
        explicit = configurable.get("user_id")
        if isinstance(explicit, str) and explicit:
            return explicit
    context = runtime.context
    if isinstance(context, Mapping):
        value = context.get("user_id")
        if isinstance(value, str) and value:
            return value
    return "dev-local"


def notice(code: str, message: str, node: str) -> GraphNotice:
    """构造稳定图警告或错误。"""

    return GraphNotice(code=code, message=message, node=node)
