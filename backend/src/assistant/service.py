"""Assistant 单轮请求的应用编排入口。

每轮依次恢复签名快照、提取结构化意图、运行对话 Agent、派生会话状态，并在用户明确
确认后调用交接服务。事实查询、模型输出解析和工单构造均由独立模块负责。
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from assistant.agent import ConversationRunner
from assistant.fact_service import AssistantEvent, AssistantFactService
from assistant.handoff import HandoffService
from assistant.intent import IntentExtractor
from assistant.language import is_explicit_submit_request
from assistant.model_factory import create_chat_model
from assistant.models import (
    AssistantDecision,
    AssistantHandoff,
    AssistantMessage,
    AssistantSnapshot,
    AssistantTurnRequest,
)
from assistant.selection import apply_action, apply_decision, update_status
from runtime.config import Settings
from runtime.contracts import AssistantDependencies
from runtime.tokens import SignedPayloadCodec

SESSION_KIND = "assistant_session"


class AssistantService:
    """协调一轮对话，不直接实现 Provider、Agent 工具或 Graph 节点。"""

    def __init__(
        self,
        dependencies: AssistantDependencies,
        settings: Settings,
        codec: SignedPayloadCodec,
    ) -> None:
        self.dependencies = dependencies
        self.settings = settings
        self.codec = codec
        self.model: BaseChatModel | None = create_chat_model(settings)
        self.handoff = HandoffService(settings, codec)

    async def turn(
        self,
        request: AssistantTurnRequest,
        user_id: str,
    ) -> list[AssistantEvent]:
        """执行完整单轮流程并返回按完成顺序排列的 SSE 事件。"""

        snapshot = self._load_snapshot(request.session_token, user_id)
        user_text = self._apply_input(snapshot, request)
        snapshot.messages = [
            *snapshot.messages,
            AssistantMessage(role="user", content=user_text),
        ][-20:]
        facts = AssistantFactService(self.dependencies, snapshot)
        explicit_submit = bool(
            request.message is not None and is_explicit_submit_request(user_text)
        )

        handoff = None
        if snapshot.status == "ready" and explicit_submit:
            reply, handoff = await self._submit(
                facts,
                user_id,
                "资料已确认，正在刷新事实并提交规划。",
            )
        else:
            decision = await self._decide(snapshot, user_text)
            apply_decision(snapshot, decision)
            update_status(snapshot)
            submit_requested = decision.submit_requested or explicit_submit
            if submit_requested and snapshot.status == "ready":
                reply, handoff = await self._submit(
                    facts,
                    user_id,
                    "资料已确认，正在刷新事实并提交规划。",
                )
            else:
                reply = await self._respond(snapshot, facts)
                if submit_requested:
                    reply, handoff = await self._submit(facts, user_id, reply)

        snapshot.messages = [
            *snapshot.messages,
            AssistantMessage(role="assistant", content=reply),
        ][-20:]
        session_token = self.codec.issue(
            SESSION_KIND,
            user_id,
            snapshot,
            self.settings.assistant_session_ttl_seconds,
        )
        return self._events(facts, snapshot, session_token, reply, handoff)

    def _load_snapshot(self, token: str | None, user_id: str) -> AssistantSnapshot:
        if token is None:
            return AssistantSnapshot()
        return self.codec.verify(token, SESSION_KIND, user_id, AssistantSnapshot)

    @staticmethod
    def _apply_input(snapshot: AssistantSnapshot, request: AssistantTurnRequest) -> str:
        if request.message is not None:
            return request.message.strip()
        assert request.action is not None
        return apply_action(snapshot, request.action)

    async def _decide(
        self,
        snapshot: AssistantSnapshot,
        user_text: str,
    ) -> AssistantDecision:
        """调用结构化意图提取边界；子类可替换为固定决策。"""

        return await IntentExtractor(self.model).decide(snapshot, user_text)

    async def _respond(
        self,
        snapshot: AssistantSnapshot,
        facts: AssistantFactService,
    ) -> str:
        """调用 LangChain Agent 边界；子类可替换为固定回复。"""

        return await ConversationRunner(self.model).respond(snapshot, facts)

    async def _submit(
        self,
        facts: AssistantFactService,
        user_id: str,
        incomplete_reply: str,
    ) -> tuple[str, AssistantHandoff | None]:
        """把完整且刷新有效的会话交给工单服务。"""

        return await self.handoff.submit(facts, user_id, incomplete_reply)

    @staticmethod
    def _events(
        facts: AssistantFactService,
        snapshot: AssistantSnapshot,
        session_token: str,
        reply: str,
        handoff: AssistantHandoff | None,
    ) -> list[AssistantEvent]:
        """事件表示已完成结果，不宣称是模型 token 或工具实时流。"""

        events = [*facts.events, ("message.completed", {"content": reply})]
        events.append(
            (
                "session.updated",
                {
                    "snapshot": snapshot.model_dump(mode="json"),
                    "session_token": session_token,
                },
            )
        )
        if handoff is not None:
            events.append(("handoff.ready", handoff.model_dump(mode="json")))
        events.append(("done", {}))
        return events
