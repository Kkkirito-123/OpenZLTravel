"""ReviewAgent、修订路由与保存前最终校验节点。

审查阶段和 Planner 阶段虽然在拓扑上相邻，但职责不同：ReviewAgent 只提出问题，
``final_validate`` 才负责检查事实边界和副作用前条件。单独放在本文件后，学习者可以
清楚地区分“模型给建议”和“确定性代码决定能否保存”。
"""

from __future__ import annotations

from typing import Any, Literal

from domain.errors import ModelUnavailableError, TravelGraphError
from domain.models import ReviewResult, TravelFacts, TravelSelection
from domain.validation import validate_draft, validate_routes, validate_selection
from runtime.contracts import TravelDependencies
from travel_graph.agents import ReviewAgent
from travel_graph.state import TravelState
from travel_graph.utils import notice


class ReviewNodes:
    """把语义审查、最多一次修订和最终确定性校验聚合为一个图阶段。"""

    def __init__(self, dependencies: TravelDependencies) -> None:
        self.reviewer = ReviewAgent(dependencies.review_model)

    async def review(self, state: TravelState) -> dict[str, Any]:
        """运行 ReviewAgent；失败时跳过语义审查，但不跳过最终校验。

        Review 只能报告节奏、可读性和需求匹配问题。无论模型是否可用，后续
        ``final_validate`` 都会重新验证草稿引用的事实 ID、选择和路线端点。
        """

        draft = state.get("draft")
        if draft is None:
            raise TravelGraphError("draft_missing", "审查前缺少行程草稿")
        try:
            result = await self.reviewer.run(
                state["requirements"],
                state.get("facts", TravelFacts()),
                draft,
            )
        except TimeoutError:
            return self._skipped_review(
                "review_timeout",
                "行程审查超时，将继续执行确定性校验。",
            )
        except ModelUnavailableError as error:
            return self._skipped_review(
                error.code,
                f"{error.message}，将继续执行确定性校验。",
            )
        except Exception:
            return self._skipped_review(
                "review_unavailable",
                "行程审查模型暂时不可用。",
            )
        update: dict[str, Any] = {"review": result}
        if not result.passed and state.get("revision_count", 0) >= 1:
            update["warnings"] = [
                notice(
                    "review_limit_reached",
                    "已完成唯一一次修订，将交由确定性校验决定是否保存。",
                    "review_agent",
                )
            ]
        return update

    @staticmethod
    def prepare_revision(state: TravelState) -> dict[str, Any]:
        """修订计数在回到 PlannerAgent 前递增，硬性保证最多一次回路。"""

        return {"revision_count": state.get("revision_count", 0) + 1, "phase": "planning"}

    @staticmethod
    def route_after_review(state: TravelState) -> Literal["revise", "finalize"]:
        """审查只能触发一次修订，不允许无限反思循环。"""

        review = state.get("review")
        if review and not review.passed and state.get("revision_count", 0) < 1:
            return "revise"
        return "finalize"

    @staticmethod
    def final_validate(state: TravelState) -> dict[str, Any]:
        """在任何 Store 写入之前执行结构、选择和事实 ID 校验。

        这是副作用前的最后安全闸门：它不写数据库、不修改 Store，只在发现不可信状态时
        抛出稳定领域错误。只有它成功后，下一节点 ``save`` 才有权限持久化行程。
        """

        draft = state.get("draft")
        if draft is None:
            raise TravelGraphError("draft_missing", "最终校验缺少行程草稿")
        requirements = state["requirements"]
        facts = state.get("facts", TravelFacts())
        selection = state.get("selection", TravelSelection())
        validate_selection(requirements, facts, selection)
        validate_draft(requirements, facts, selection, draft)
        validate_routes(facts)
        return {}

    @staticmethod
    def _skipped_review(code: str, message: str) -> dict[str, Any]:
        """模型不可用时保留可序列化的“已跳过”结果，继续确定性流程。"""

        return {
            "review": ReviewResult(passed=True, issues=[]),
            "warnings": [notice(code, message, "review_agent")],
        }
