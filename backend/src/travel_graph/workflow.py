"""单根 TravelGraph 的拓扑组装。

图本身而非 LLM Supervisor 决定所有路由：Agent 只做需求理解、草稿规划和审查，
Provider 节点只写事实，保存前必须经过确定性校验。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from runtime.contracts import TravelDependencies
from travel_graph.nodes.discovery import DiscoveryNodes
from travel_graph.nodes.persistence import PersistenceNodes
from travel_graph.nodes.planning import PlanningNodes
from travel_graph.nodes.requirement import RequirementNodes
from travel_graph.nodes.review import ReviewNodes
from travel_graph.state import TravelContext, TravelInput, TravelState


def build_travel_graph(
    dependencies: TravelDependencies,
    *,
    checkpointer: Any = None,
    store: Any = None,
) -> Any:
    """编译唯一的 ``travel`` 根图，测试可注入内存 Checkpointer 和 Store。

    阅读这个函数时可以把它看成一张“有向流程图的施工图”：``add_node`` 注册节点，
    ``add_edge`` 表示无条件下一跳，``add_conditional_edges`` 根据确定性路由函数选择
    下一跳，列表形式的边则表示并行扇出或等待多个节点汇合。

    这里故意不使用 LLM Supervisor。Agent 只负责受限的理解、规划和审查，是否追问、
    是否查数据、是否修订和何时保存，都由显式拓扑决定，便于重放、测试和学习。
    """

    requirements = RequirementNodes(dependencies)
    discovery = DiscoveryNodes(dependencies)
    planning = PlanningNodes(dependencies)
    review = ReviewNodes(dependencies)
    persistence = PersistenceNodes()
    builder = StateGraph(
        TravelState,
        context_schema=TravelContext,
        input_schema=TravelInput,
    )

    # 第一段：把自然语言变成结构化需求；能用规则理解时跳过 RequirementAgent。
    builder.add_node("parse_requirement", requirements.parse)
    builder.add_node("requirement_agent", requirements.recognize)
    builder.add_node("requirement_guard", _pass_node(), input_schema=TravelState)
    builder.add_node("clarification", requirements.clarification)
    builder.add_node("recommend_destination", requirements.recommend)
    builder.add_node("destination_selection", requirements.choose_destination)
    # 第二段：目的地确定后先准备 Catalog，后续 Provider 只能读取这里产生的事实。
    builder.add_node("prepare_catalog", discovery.prepare_catalog)
    builder.add_node("discovery_fanout", _pass_node(), input_schema=TravelState)
    builder.add_node("fetch_rail", discovery.fetch_rail)
    builder.add_node("fetch_hotels", discovery.fetch_hotels)
    builder.add_node("fetch_weather", discovery.fetch_weather)
    builder.add_node("evidence_guard", discovery.evidence_guard)
    builder.add_node("travel_selection", discovery.select_travel)
    # 第三段：用户选择事实后才允许 Planner 生成草稿；草稿后仍有路线、预算和校验。
    builder.add_node("planner_agent", planning.plan)
    builder.add_node("build_routes", planning.build_routes)
    builder.add_node("calculate_budget", planning.budget)
    builder.add_node("review_agent", review.review)
    builder.add_node("prepare_revision", review.prepare_revision)
    builder.add_node("final_validator", review.final_validate)
    builder.add_node("save_trip", persistence.save)
    builder.add_node("failed", requirements.fail_unsupported)

    builder.add_edge(START, "parse_requirement")
    builder.add_conditional_edges(
        "parse_requirement",
        requirements.route_after_parse,
        {"agent": "requirement_agent", "gate": "requirement_guard"},
    )
    builder.add_edge("requirement_agent", "requirement_guard")
    builder.add_conditional_edges(
        "requirement_guard",
        requirements.route_after_requirement,
        {
            "clarification": "clarification",
            "destination": "recommend_destination",
            "discovery": "prepare_catalog",
            "failed": "failed",
        },
    )
    builder.add_edge("clarification", "requirement_guard")
    builder.add_conditional_edges(
        "recommend_destination",
        requirements.route_after_recommendation,
        {"selection": "destination_selection", "failed": "failed"},
    )
    builder.add_edge("destination_selection", "prepare_catalog")
    builder.add_conditional_edges(
        "prepare_catalog",
        discovery.route_after_catalog,
        {"discover": "discovery_fanout", "failed": "failed"},
    )

    # Catalog 完成后扇出三类独立 I/O，数据全部到齐后再开放选择中断。
    for node in ("fetch_rail", "fetch_hotels", "fetch_weather"):
        builder.add_edge("discovery_fanout", node)
    builder.add_edge(
        ["fetch_rail", "fetch_hotels", "fetch_weather"],
        "evidence_guard",
    )
    builder.add_edge("evidence_guard", "travel_selection")
    builder.add_edge("travel_selection", "planner_agent")
    builder.add_edge("planner_agent", "build_routes")
    builder.add_edge("build_routes", "calculate_budget")
    builder.add_edge("calculate_budget", "review_agent")
    builder.add_conditional_edges(
        "review_agent",
        review.route_after_review,
        {"revise": "prepare_revision", "finalize": "final_validator"},
    )
    builder.add_edge("prepare_revision", "planner_agent")
    # 最后两步必须保持顺序：只有 final_validator 成功，save_trip 才能产生 Store 副作用。
    builder.add_edge("final_validator", "save_trip")
    builder.add_edge("save_trip", END)
    builder.add_edge("failed", END)
    return builder.compile(checkpointer=checkpointer, store=store, name="travel")


def _pass(_state: TravelState) -> dict[str, Any]:
    """显式汇合/守卫节点，使拓扑保持可读而不引入隐式路由。"""

    return {}


def _pass_node() -> Any:
    """LangGraph 当前类型重载无法推断 total=False TypedDict 空更新节点。"""

    return _pass
