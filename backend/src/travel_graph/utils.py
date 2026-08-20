"""Graph 节点共享的小型、无业务分支工具。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from travel_graph.state import GraphNotice, TravelContext


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


def thread_id_from(config: RunnableConfig) -> str:
    """返回 Checkpointer 使用的 thread_id，保存幂等键与其绑定。"""

    configurable = config.get("configurable", {})
    if isinstance(configurable, Mapping):
        value = configurable.get("thread_id")
        if isinstance(value, str) and value:
            return value
    return "unscoped"


def message_text(message: object) -> str:
    """从 LangChain 消息或 SDK 字典中取出纯文本。"""

    if isinstance(message, BaseMessage):
        return _content_text(message.content)
    if isinstance(message, Mapping):
        return _content_text(message.get("content"))
    return ""


def latest_message_text(messages: list[object]) -> str:
    """返回最后一条非空消息文本。"""

    return next((text for item in reversed(messages) if (text := message_text(item))), "")


def notice(code: str, message: str, node: str) -> GraphNotice:
    """构造稳定图警告或错误。"""

    return GraphNotice(code=code, message=message, node=node)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        values = [
            item.get("text", "")
            for item in content
            if isinstance(item, Mapping) and isinstance(item.get("text"), str)
        ]
        return " ".join(values).strip()
    return ""
