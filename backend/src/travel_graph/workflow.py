"""TravelOrder 驱动的轻量确定性规划图。

图的输入只有 ``{"order_token": "..."}``，不接受旧式用户消息或快速表单。节点依次
完成工单验证、日程生成、路线查询、预算计算、最终校验和路线确认；确认后才幂等保存
行程。除 ``build_routes`` 使用注入的 RouteGateway 外，图不会重新访问 Catalog、铁路、
酒店或天气 Provider。

``build_travel_graph`` 是唯一的图组合根。节点实现只接收领域模型、运行时 Protocol 和
签名令牌，具体 Provider 的装配由 ``runtime.container`` 完成。
"""

from __future__ import annotations

from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph

from runtime.contracts import PlanningDependencies
from runtime.tokens import SignedPayloadCodec
from travel_graph.nodes.confirmation import ConfirmationNodes
from travel_graph.nodes.order import OrderNodes
from travel_graph.nodes.persistence import PersistenceNodes
from travel_graph.nodes.planning import PlanningNodes
from travel_graph.state import TravelContext, TravelInput, TravelState


def build_travel_graph(
    dependencies: PlanningDependencies,
    codec: SignedPayloadCodec,
    *,
    checkpointer: Any = None,
    store: Any = None,
) -> Any:
    """编译唯一的 ``travel`` 工单规划图。

    ``checkpointer`` 和 ``store`` 由 LangGraph Server 注入：前者保存运行中 Thread 的
    恢复点，后者只保存最终行程。二者都不承载 Assistant 会话，也不替代
    ``TravelOrderToken`` 的身份和有效期校验。
    """

    orders = OrderNodes(codec)
    planning = PlanningNodes(dependencies)
    confirmation = ConfirmationNodes()
    persistence = PersistenceNodes()
    builder = StateGraph(
        TravelState,
        context_schema=TravelContext,
        input_schema=TravelInput,
    )
    builder.add_node("validate_order", cast(Any, orders.validate), input_schema=TravelInput)
    builder.add_node("build_itinerary", planning.plan)
    builder.add_node("build_routes", planning.build_routes)
    builder.add_node("calculate_budget", planning.budget)
    builder.add_node("validate_plan", confirmation.validate_plan)
    builder.add_node("route_preview", confirmation.preview)
    builder.add_node("save_trip", persistence.save)

    builder.add_conditional_edges(
        START,
        _route_after_start,
        {"active": "validate_order", "completed": END},
    )
    builder.add_edge("validate_order", "build_itinerary")
    builder.add_edge("build_itinerary", "build_routes")
    builder.add_edge("build_routes", "calculate_budget")
    builder.add_edge("calculate_budget", "validate_plan")
    builder.add_edge("validate_plan", "route_preview")
    builder.add_conditional_edges(
        "route_preview",
        confirmation.route_after_preview,
        {"revise": "build_itinerary", "save": "save_trip"},
    )
    builder.add_edge("save_trip", END)
    return builder.compile(checkpointer=checkpointer, store=store, name="travel")


def _route_after_start(state: TravelState) -> Literal["active", "completed"]:
    return "completed" if state.get("phase") == "completed" else "active"
