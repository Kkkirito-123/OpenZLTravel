"""LangGraph Agent Server 加载的唯一应用组合根。

只有本模块同时知道图编排与具体依赖装配。领域层、Graph 节点和 Provider 通过 Protocol
协作，环境变量、网络客户端和 Web 认证不会进入业务节点。Assistant Service 有独立的
组合根，不在这里导入或启动。
"""

from __future__ import annotations

from runtime.config import get_settings
from runtime.container import get_planning_dependencies
from runtime.tokens import SignedPayloadCodec
from travel_graph.workflow import build_travel_graph

# 模块导入时只做一次“依赖装配 + 图编译”。
# Agent Server 会在每次执行时注入自身的 Checkpointer 和 Store；这里不处理用户消息，
# 也不直接调用任何 Provider。真正的节点顺序请继续阅读 travel_graph/workflow.py。
_settings = get_settings()
travel = build_travel_graph(
    get_planning_dependencies(_settings),
    SignedPayloadCodec(_settings.signing_secret),
)
