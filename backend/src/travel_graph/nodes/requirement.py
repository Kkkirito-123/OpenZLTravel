"""需求收集、记忆和目的地选择节点。

这里是学习 LangGraph ``Node`` 的第一组示例。每个方法都遵循同一个小契约：

1. 从 ``TravelState`` 读取本节点需要的字段；
2. 只完成一个职责（解析、追问、推荐或选择）；
3. 返回一个“状态增量”，而不是重新构造完整 State；
4. 由 ``workflow.py`` 中的 Edge 决定下一跳。

这样拆分的好处是：需求规则、模型调用和用户交互边界彼此独立，初学者可以先读
``parse`` 和路由函数，再逐步理解 Agent 与 ``interrupt/resume``。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_config
from langgraph.runtime import Runtime

from domain.errors import ModelUnavailableError, ResumeValidationError
from domain.models import RequirementPatch, TravelRequirements, TravelSelection
from domain.parsing import FastParseResult, merge_requirements, parse_fast_requirements
from runtime.contracts import TravelDependencies
from travel_graph.agents import RequirementAgent
from travel_graph.interrupts import (
    ClarificationInterrupt,
    ClarificationResume,
    DestinationSelectionInterrupt,
    DestinationSelectionResume,
    interrupt_until_valid,
    validate_resume,
)
from travel_graph.state import TravelContext, TravelState
from travel_graph.utils import latest_message_text, message_text, notice, user_id_from

RouteAfterRequirement = Literal["clarification", "destination", "discovery", "failed"]


class RequirementNodes:
    """把规则解析、受限 Agent 和两类需求 interrupt 聚合在一处。

    这个类不是“总管 Agent”。它只是把同一阶段的节点绑定到共享依赖上，方便
    ``workflow.py`` 注册方法；真正的流程控制仍由图的边负责。
    """

    def __init__(self, dependencies: TravelDependencies) -> None:
        self.dependencies = dependencies
        self.agent = RequirementAgent(dependencies.requirement_model)

    async def parse(
        self,
        state: TravelState,
        runtime: Runtime[TravelContext],
    ) -> dict[str, Any]:
        """先合并授权记忆，再用本轮明确值覆盖，不触发 LLM。

        输入：最近一条用户消息、当前需求和可选的长期偏好。
        处理：快速规则解析；本轮明确字段优先级最高；只有显式“记住/忘记”才写 Store。
        输出：需求、初步意图和 ``fast_understood`` 标记。
        下一跳：``route_after_parse`` 根据标记决定是否需要 RequirementAgent。
        """

        # State 中的 messages 可能来自不同消息实现，因此这里只把它当作可迭代对象，
        # 不把 LangChain 的具体消息类泄漏到领域解析器中。
        messages = cast(list[object], state.get("messages", []))
        text = latest_message_text(messages)
        parsed = parse_fast_requirements(text)
        config = get_config()
        # 偏好是低优先级默认值；本轮用户明确说出的内容会在 merge_requirements 中覆盖它。
        current = state.get("requirements") or await self._load_preferences(config, runtime)
        requirements = merge_requirements(current, parsed.patch)
        await self._update_preferences(parsed, requirements, config, runtime)
        intent = "destination_discovery" if not requirements.destination else "trip_planning"
        return {
            "phase": "collecting",
            "requirements": requirements,
            "selection": state.get("selection") or TravelSelection(),
            "revision_count": 0,
            "fast_understood": parsed.understood,
            "intent": intent,
        }

    async def recognize(self, state: TravelState) -> dict[str, Any]:
        """仅当快速规则完全无法理解时调用 RequirementAgent。

        Agent 的输入被刻意限制为“当前消息 + 结构化需求 + 最近几轮”，避免把完整历史
        当成隐式数据库。超时、结构化输出错误或模型不可用都转成 warning，让确定性追问
        接管流程，而不是把一次识别失败升级成 HTTP 504。
        """

        requirements = state.get("requirements", TravelRequirements())
        messages = cast(list[object], state.get("messages", []))
        text = latest_message_text(messages)
        recent = [message_text(item) for item in messages[-4:]]
        try:
            result = await self.agent.run(text, requirements, recent)
        except TimeoutError:
            return {
                "warnings": [
                    notice(
                        "requirement_timeout",
                        "需求识别超时，已切换为确定性追问。",
                        "requirement_agent",
                    )
                ]
            }
        except ModelUnavailableError as error:
            return {
                "warnings": [notice(error.code, error.message, "requirement_agent")]
            }
        except Exception:
            return {
                "warnings": [
                    notice(
                        "requirement_unavailable",
                        "需求模型暂时不可用，已切换为确定性追问。",
                        "requirement_agent",
                    )
                ]
            }
        return {
            "requirements": merge_requirements(requirements, result.patch),
            "intent": result.intent,
        }

    def clarification(self, state: TravelState) -> dict[str, Any]:
        """暂停并等待结构化需求补丁；类型错误时不推进状态。

        ``interrupt_until_valid`` 会在同一节点内重复展示问题，直到收到符合
        ``ClarificationResume`` 的载荷。只有验证成功后才返回状态增量，因此错误恢复
        不会污染 Checkpoint 中的需求。
        """

        requirements = state.get("requirements", TravelRequirements())
        missing = requirements.missing_fields()
        payload = ClarificationInterrupt(
            question=_clarification_question(missing),
            missing_fields=missing,
        )

        def validate(raw: object) -> TravelRequirements:
            """校验本次补丁确实推进了需求，空回答继续停留在同一中断。"""

            resume = validate_resume(raw, ClarificationResume, "clarification")
            updated = merge_requirements(requirements, resume.values)
            if updated.model_dump() == requirements.model_dump():
                raise ResumeValidationError(
                    "empty_resume_payload",
                    "需求恢复载荷没有提供任何新值",
                )
            return updated

        updated = interrupt_until_valid(payload, validate)
        return {"requirements": updated}

    async def recommend(self, state: TravelState) -> dict[str, Any]:
        """从目录中生成最多五个可解释的真实城市候选。

        推荐是确定性 Catalog 能力，不让 LLM 凭空创造城市。候选必须来自目录返回的
        真实 ``city`` 和 ``candidate_id``，后续选择节点只接受这些 ID。
        """

        requirements = state["requirements"]
        if not requirements.origin or not requirements.region:
            return {
                "phase": "failed",
                "errors": [
                    notice(
                        "recommendation_input_missing",
                        "目的地推荐缺少出发地或地区。",
                        "recommend_destination",
                    )
                ],
            }
        try:
            candidates = await self.dependencies.catalog.recommend_destinations(
                requirements.origin,
                requirements.region,
                requirements.preferences,
                limit=5,
            )
        except Exception:
            return {
                "phase": "failed",
                "errors": [
                    notice(
                        "destination_unavailable",
                        "目的地目录暂时不可用。",
                        "recommend_destination",
                    )
                ],
            }
        if not candidates:
            return {
                "phase": "failed",
                "errors": [
                    notice(
                        "destination_not_found",
                        "指定地区暂无可用城市候选。",
                        "recommend_destination",
                    )
                ],
            }
        ordered = sorted(candidates, key=lambda item: (-item.score, item.city.name))[:5]
        return {"destination_candidates": ordered}

    def choose_destination(self, state: TravelState) -> dict[str, Any]:
        """用户选择必须精确命中当前 interrupt 公开的候选 ID。

        这是“暂停点”的另一种写法：先把候选列表放进 interrupt，再把用户恢复的 ID
        映射回目录对象，最后只把城市名称写回结构化需求。
        """

        candidates = state.get("destination_candidates", [])
        payload = DestinationSelectionInterrupt(candidates=candidates)

        def validate(raw: object) -> Any:
            """只接受当前载荷公开的候选 ID，防止恢复时注入任意城市。"""

            resume = validate_resume(raw, DestinationSelectionResume, "destination_selection")
            selected = next(
                (item for item in candidates if item.candidate_id == resume.candidate_id),
                None,
            )
            if selected is None:
                raise ResumeValidationError(
                    "unknown_destination_candidate",
                    "选择的目的地不在当前候选列表中",
                )
            return selected

        selected = interrupt_until_valid(payload, validate)
        patch = RequirementPatch(destination=selected.city.name)
        return {
            "requirements": merge_requirements(state["requirements"], patch),
            "intent": "trip_planning",
        }

    @staticmethod
    def route_after_parse(state: TravelState) -> Literal["agent", "gate"]:
        """高置信规则命中时不浪费一次 LLM 调用。"""

        return "gate" if state.get("fast_understood") else "agent"

    @staticmethod
    def route_after_requirement(state: TravelState) -> RouteAfterRequirement:
        """用确定性字段完整性决定下一跳，不让 LLM 控制路由。"""

        if state.get("intent") == "unsupported":
            return "failed"
        requirements = state.get("requirements", TravelRequirements())
        if requirements.missing_fields():
            return "clarification"
        if not requirements.destination:
            return "destination"
        return "discovery"

    @staticmethod
    def route_after_recommendation(state: TravelState) -> Literal["selection", "failed"]:
        """目录失败时终止，不向用户展示空选择框。"""

        return "failed" if state.get("phase") == "failed" else "selection"

    @staticmethod
    def fail_unsupported(state: TravelState) -> dict[str, Any]:
        """对非旅行请求输出稳定终止状态。"""

        if state.get("phase") == "failed":
            return {}
        return {
            "phase": "failed",
            "errors": [
                notice(
                    "unsupported_intent",
                    "当前只支持旅行需求收集与规划。",
                    "requirement_guard",
                )
            ],
        }

    async def _load_preferences(
        self,
        config: RunnableConfig,
        runtime: Runtime[TravelContext],
    ) -> TravelRequirements:
        """读取当前用户明确授权的稳定偏好，并作为本轮需求的低优先级默认值。

        偏好与行程历史使用不同 Store 命名空间；即使 Store 中出现多余字段，也要再次经过
        白名单过滤。随后本轮消息解析出的明确值会覆盖这里的默认值。
        """

        store = runtime.store
        if store is None:
            return TravelRequirements()
        item = await store.aget((user_id_from(config, runtime), "preferences"), "stable")
        if item is None or not isinstance(item.value, Mapping):
            return TravelRequirements()
        allowed = _allowed_preferences(dict(item.value))
        return merge_requirements(TravelRequirements(), RequirementPatch.model_validate(allowed))

    async def _update_preferences(
        self,
        parsed: FastParseResult,
        requirements: TravelRequirements,
        config: RunnableConfig,
        runtime: Runtime[TravelContext],
    ) -> None:
        """只响应明确的“记住/忘记”命令，普通需求绝不产生长期偏好副作用。

        ``remember_fields`` 和 ``forget_fields`` 均由确定性规则解析产生；Agent 不能授权自己
        写入 Store。空偏好集合会删除固定 key，避免保留无意义记录。
        """

        store = runtime.store
        if store is None or (not parsed.remember_fields and not parsed.forget_fields):
            return
        namespace = (user_id_from(config, runtime), "preferences")
        existing = await store.aget(namespace, "stable")
        values = dict(existing.value) if existing and isinstance(existing.value, Mapping) else {}
        for field in parsed.remember_fields:
            value = getattr(requirements, field, None)
            if value not in (None, [], ""):
                values[field] = value
        for field in parsed.forget_fields:
            values.pop(field, None)
        if values:
            await store.aput(namespace, "stable", _allowed_preferences(values))
        else:
            await store.adelete(namespace, "stable")


def _allowed_preferences(values: dict[str, Any]) -> dict[str, Any]:
    """把长期记忆限制在产品明确允许且相对稳定的偏好字段。"""

    allowed = {
        "origin",
        "preferences",
        "dietary_preferences",
        "pace",
        "hotel_level",
        "transport_mode",
    }
    return {key: value for key, value in values.items() if key in allowed}


def _clarification_question(missing: list[str]) -> str:
    """把内部字段名转换为稳定、可直接展示的中文追问。"""

    labels = {
        "origin": "出发地",
        "destination_or_region": "具体目的地或希望探索的地区",
        "start_date": "开始日期",
        "end_date": "结束日期或行程天数",
    }
    readable = "、".join(labels.get(field, field) for field in missing)
    return f"请补充：{readable}。"
