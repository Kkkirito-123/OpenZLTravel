"""LangGraph Agent Server 加载的唯一应用组合根。

只有本模块同时知道图编排与具体 Provider。领域层、Agent 和 Provider 彼此通过 Protocol
协作，避免把环境变量、网络客户端或 Web 认证混入业务节点。
"""

from __future__ import annotations

from runtime.container import get_dependencies
from travel_graph.workflow import build_travel_graph

# 这是新手应该首先理解的入口：模块导入时只做一次“依赖装配 + 图编译”。
# Agent Server 会在每次执行时注入自身的 Checkpointer 和 Store；这里不处理用户消息，
# 也不直接调用任何 Provider。真正的节点顺序请继续阅读 travel_graph/workflow.py。
travel = build_travel_graph(get_dependencies())
