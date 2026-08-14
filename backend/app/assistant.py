"""多轮旅行助手的应用服务。

本模块连接受限意图命令、权威旅行状态、OpenZLAgent 会话上下文和现有规划运行时。
供应商事实仍由规划会话查询；聊天模型没有调用工具或修改供应商结果的权限。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

from re_zlagent.harness.context import (  # type: ignore[import-untyped]
    ContextManifest,
    ContextRef,
    MemoryRef,
)
from re_zlagent.harness.conversation import (  # type: ignore[import-untyped]
    ConversationContext,
)

from app.dialogue import (
    CommandEffects,
    CommandGeneration,
    TravelCommandGenerator,
    TravelContextAssembler,
    apply_commands,
    apply_memory_defaults,
    decide_next,
    parse_fast_commands,
    validate_memory_commands,
)
from app.errors import AppError
from app.models import (
    AssistantConversationTurn,
    AssistantMessageRequest,
    AssistantSessionView,
    AssistantTokenUsage,
    AssistantTurnResponse,
    MemorySlotName,
    PlanningRequest,
    PlanningSession,
    TravelDialogueState,
    TravelMemory,
)
from app.skills import get_skill
from app.storage import DialogueRepository

LOGGER = logging.getLogger("openzltravel.assistant")


class CityResolver(Protocol):
    """验证具体城市时使用的本地目录边界。"""

    def resolve_city(self, destination: str) -> Any:
        """返回规范城市，无法确认时抛出 LookupError。"""


class PlanningStarter(Protocol):
    """创建现有三阶段规划会话所需的最小运行时边界。"""

    def start(
        self, request: PlanningRequest, idempotency_key: str | None = None
    ) -> PlanningSession:
        """立即创建规划会话，不等待外部查询结束。"""


class ConversationPort(Protocol):
    """助手使用的 OpenZLAgent 会话管理最小边界。"""

    async def prepare(self, session_id: str) -> ConversationContext:
        """读取受限近期上下文。"""

    async def record_turn(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> object:
        """保存一轮完整对话，并按策略尝试滚动摘要。"""


class TravelAssistantService:
    """串行处理每个会话，并保持消息幂等和失败不改状态。"""

    def __init__(
        self,
        repository: DialogueRepository,
        conversations: ConversationPort,
        city_resolver: CityResolver,
        planning_runtime: PlanningStarter,
        command_generator: TravelCommandGenerator | None,
        session_token_limit: int = 20_000,
    ) -> None:
        self._repository = repository
        self._conversations = conversations
        self._city_resolver = city_resolver
        self._planning_runtime = planning_runtime
        self._command_generator = command_generator
        self._context = TravelContextAssembler()
        self._session_token_limit = session_token_limit
        self._locks: dict[UUID, asyncio.Lock] = {}

    def create(self) -> AssistantSessionView:
        """创建空会话；首条用户消息再决定进入哪一个旅行 Flow。"""

        now = _now()
        state = TravelDialogueState(
            session_id=uuid4(),
            created_at=now,
            updated_at=now,
        )
        memories = tuple(self._repository.list_memories())
        state = apply_memory_defaults(state, memories)
        self._repository.create_dialogue(state)
        return AssistantSessionView(
            state=state,
            skill=None,
            memories=list(memories),
        )

    async def get(self, session_id: UUID) -> AssistantSessionView:
        """读取权威状态和仍在短期窗口中的完整对话轮次。"""

        state = self._require_state(session_id)
        context = await self._conversations.prepare(str(session_id))
        turns = [
            AssistantConversationTurn(
                sequence=turn.sequence,
                user_content=turn.user_content,
                assistant_content=turn.assistant_content,
                created_at=turn.created_at,
            )
            for turn in context.recent_turns
        ]
        skill = get_skill(state.active_flow)
        return AssistantSessionView(
            state=state,
            turns=turns,
            skill=skill.view() if skill else None,
            memories=self._repository.list_memories(),
        )

    def list_memories(self) -> list[TravelMemory]:
        """返回用户明确保存的长期旅行偏好。"""

        return self._repository.list_memories()

    def delete_memory(self, key: MemorySlotName) -> None:
        """通过设置页删除长期偏好，不静默改写已经存在的会话。"""

        if not self._repository.delete_memory(key):
            raise AppError("assistant_memory_not_found", "这项长期偏好不存在", 404)

    async def send(
        self, session_id: UUID, request: AssistantMessageRequest
    ) -> AssistantTurnResponse:
        """处理一条消息；同一 message_id 永远复用第一次成功响应。"""

        async with self._lock(session_id):
            state = self._require_state(session_id)
            cached = self._repository.get_dialogue_response(session_id, request.message_id)
            if cached is not None:
                return self._resolve_cached(cached, request.content)
            return await self._process(state, request)

    async def _process(
        self, state: TravelDialogueState, request: AssistantMessageRequest
    ) -> AssistantTurnResponse:
        context = await self._conversations.prepare(str(state.session_id))
        memories = tuple(self._repository.list_memories())
        batch = parse_fast_commands(request.content, state)
        generation: CommandGeneration | None = None
        if batch is None:
            if self._command_generator is None:
                raise AppError(
                    "intent_not_configured",
                    "尚未配置意图识别模型，请先填写 LLM 配置",
                    503,
                )
            generation = await self._command_generator.generate(
                request.content,
                state,
                context,
                memories,
                self._session_token_limit - state.token_usage.total_tokens,
            )
            batch = generation.batch

        validate_memory_commands(request.content, batch)
        updated, effects = apply_commands(state, batch, self._city_resolver)
        if generation is not None:
            updated = updated.model_copy(
                update={"token_usage": _add_usage(state.token_usage, generation.usage)}
            )
        decision = decide_next(updated, effects)
        final_state = decision.state
        planning_id: UUID | None = None
        if decision.planning_request is not None:
            planning = self._planning_runtime.start(
                decision.planning_request,
                f"assistant:{state.session_id}:{final_state.revision}",
            )
            planning_id = planning.session_id
            final_state = final_state.model_copy(
                update={
                    "status": "planning_started",
                    "planning_session_id": planning_id,
                    "updated_at": _now(),
                }
            )

        skill = get_skill(final_state.active_flow)
        response = AssistantTurnResponse(
            message_id=request.message_id,
            reply=decision.reply,
            state=final_state,
            missing_slots=list(decision.missing_slots),
            planning_session_id=planning_id,
            skill=skill.view() if skill else None,
            command_source=(generation.source if generation else "fast_parser"),
            context_tokens=(generation.manifest.estimated_tokens if generation else 0),
        )
        cached_response = self._save_response(request, response, effects)
        if cached_response is not None:
            return cached_response
        manifest = (
            generation.manifest
            if generation
            else self._context.build(state, context, memories)
        )
        await self._record_turn(
            request.content,
            response.reply,
            context,
            manifest,
            generation,
            memories,
        )
        return response

    def _save_response(
        self,
        request: AssistantMessageRequest,
        response: AssistantTurnResponse,
        effects: CommandEffects,
    ) -> AssistantTurnResponse | None:
        try:
            self._repository.save_dialogue_response(
                response.state,
                request.message_id,
                request.content,
                response,
                effects.memory_upserts,
                set(effects.memory_deletes),
            )
            return None
        except sqlite3.IntegrityError as error:
            cached = self._repository.get_dialogue_response(
                response.state.session_id, request.message_id
            )
            if cached is not None:
                return self._resolve_cached(cached, request.content)
            raise AppError(
                "assistant_message_conflict",
                "会话刚刚被另一条消息更新，请刷新后重试",
                409,
            ) from error

    async def _record_turn(
        self,
        user_content: str,
        assistant_content: str,
        context: ConversationContext,
        manifest: ContextManifest,
        generation: CommandGeneration | None,
        memories: tuple[TravelMemory, ...],
    ) -> None:
        metadata = _turn_metadata(context, manifest, generation, memories)
        try:
            await self._conversations.record_turn(
                context.session_id,
                user_content,
                assistant_content,
                metadata=metadata,
            )
        except Exception as error:  # noqa: BLE001 - 对话摘要不能推翻已保存任务状态
            LOGGER.warning(
                "conversation_record_failed session_id=%s error=%s",
                context.session_id,
                type(error).__name__,
            )

    def _require_state(self, session_id: UUID) -> TravelDialogueState:
        state = self._repository.get_dialogue(session_id)
        if state is None:
            raise AppError(
                "assistant_session_not_found", "旅行助手会话不存在", 404
            )
        return state

    @staticmethod
    def _resolve_cached(
        cached: tuple[str, AssistantTurnResponse], content: str
    ) -> AssistantTurnResponse:
        original, response = cached
        if original == content:
            return response
        raise AppError(
            "assistant_message_conflict",
            "同一个 message_id 不能用于不同消息",
            409,
        )

    def _lock(self, session_id: UUID) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock


def _turn_metadata(
    context: ConversationContext,
    manifest: ContextManifest,
    generation: CommandGeneration | None,
    memories: tuple[TravelMemory, ...],
) -> dict[str, Any]:
    """只保存上下文定位和模型用量，不复制提示词或供应商原始响应。"""

    context_ref = ContextRef(
        session_id=context.session_id,
        summary_id=context.summary_id,
        summary_through=context.summarized_through_sequence,
        turn_seqs=tuple(turn.sequence for turn in context.recent_turns),
        memory_refs=tuple(
            MemoryRef(id=f"travel:{item.key}", version=item.version)
            for item in memories
        ),
        manifest=manifest.to_dict(),
    )
    metadata: dict[str, Any] = {
        "command_source": "llm" if generation else "fast_parser",
        "context_ref": context_ref.to_dict(),
    }
    if generation is not None:
        metadata["model"] = _safe_model_metadata(generation.metadata)
        metadata["usage"] = generation.usage.model_dump()
    return metadata


def _safe_model_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    """从模型结果中挑选非正文可观测字段。"""

    result = {
        key: raw[key]
        for key in ("provider", "model", "finish_reason")
        if isinstance(raw.get(key), str)
    }
    budget = raw.get("token_budget")
    if isinstance(budget, dict):
        result["token_budget"] = {
            key: value
            for key, value in budget.items()
            if isinstance(value, (bool, int, str, type(None)))
        }
    for key in ("model_calls", "result_cache"):
        value = raw.get(key)
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            result[key] = value
    prompt_cache = raw.get("prompt_cache")
    if isinstance(prompt_cache, dict):
        result["prompt_cache"] = {
            key: value
            for key, value in prompt_cache.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
    return result


def _add_usage(
    current: AssistantTokenUsage, added: AssistantTokenUsage
) -> AssistantTokenUsage:
    """累加成功意图调用的供应商计量或保守估算。"""

    return AssistantTokenUsage(
        model_calls=current.model_calls + added.model_calls,
        input_tokens=current.input_tokens + added.input_tokens,
        output_tokens=current.output_tokens + added.output_tokens,
        cached_input_tokens=current.cached_input_tokens + added.cached_input_tokens,
        total_tokens=current.total_tokens + added.total_tokens,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
