"""旅行助手的受限命令、专属上下文与确定性对话策略。

本模块只理解和更新用户需求，不访问车票、酒店、地图或其他供应商。LLM 只能生成
受限命令；是否追问、何时创建规划会话由确定性策略决定。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Protocol, cast, get_args
from xml.sax.saxutils import escape

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from re_zlagent.harness.context import (  # type: ignore[import-untyped]
    ContextInput,
    ContextManifest,
    ContextManifestBuilder,
    ContextTrust,
)
from re_zlagent.harness.conversation import (  # type: ignore[import-untyped]
    ConversationContext,
)
from re_zlagent.harness.model import (  # type: ignore[import-untyped]
    ModelCallBudget,
    ModelClient,
    ModelMessage,
    ModelResponse,
    TokenBudgetExceededError,
    aggregate_token_usage,
    complete_with_budget,
    estimate_message_tokens,
)
from re_zlagent.harness.model.openai_compatible import (  # type: ignore[import-untyped]
    ChatCompletionTransport,
    UrllibChatCompletionTransport,
)

from app.errors import AppError
from app.models import (
    AssistantFlow,
    AssistantTokenUsage,
    MemorySlotName,
    PlanningRequest,
    SlotMetadata,
    TravelDialogueSlots,
    TravelDialogueState,
    TravelMemory,
)
from app.skills import get_skill

SlotName = Literal[
    "origin",
    "destination_region",
    "destination_city",
    "start_date",
    "end_date",
    "days",
    "budget",
    "travelers",
    "preferences",
    "dietary_preferences",
    "distance_preference",
    "pace",
    "hotel_level",
    "transport_mode",
    "notes",
]
SLOT_NAMES = frozenset(get_args(SlotName))
MEMORY_SLOT_NAMES = frozenset(get_args(MemorySlotName))


class _StrictCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartFlowCommand(_StrictCommand):
    """开始一个受支持的旅行流程。"""

    type: Literal["start_flow"]
    flow: AssistantFlow


class SetSlotCommand(_StrictCommand):
    """设置一个允许由用户提供的旅行槽位。"""

    type: Literal["set_slot"]
    name: SlotName
    value: Any
    evidence: str = Field(default="", max_length=200)


class ClearSlotCommand(_StrictCommand):
    """只在用户明确撤销信息时清空槽位。"""

    type: Literal["clear_slot"]
    name: SlotName


class ConfirmCommand(_StrictCommand):
    """确认当前流程。"""

    type: Literal["confirm"]


class CancelFlowCommand(_StrictCommand):
    """取消当前流程。"""

    type: Literal["cancel_flow"]


class RouteToChatCommand(_StrictCommand):
    """表示消息不属于当前支持的旅行流程。"""

    type: Literal["route_to_chat"]


class RememberSlotCommand(_StrictCommand):
    """在用户明确要求时保存一项稳定旅行偏好。"""

    type: Literal["remember_slot"]
    name: MemorySlotName
    value: Any
    evidence: str = Field(default="", max_length=200)


class ForgetMemoryCommand(_StrictCommand):
    """在用户明确要求时删除一项长期偏好。"""

    type: Literal["forget_memory"]
    name: MemorySlotName


TravelCommand = Annotated[
    StartFlowCommand
    | SetSlotCommand
    | ClearSlotCommand
    | ConfirmCommand
    | CancelFlowCommand
    | RouteToChatCommand
    | RememberSlotCommand
    | ForgetMemoryCommand,
    Field(discriminator="type"),
]


class TravelCommandBatch(BaseModel):
    """一次模型调用产生的有限命令序列。"""

    model_config = ConfigDict(extra="forbid")
    commands: list[TravelCommand] = Field(min_length=1, max_length=10)


class CityResolver(Protocol):
    """用于验证具体目的城市的最小目录接口。"""

    def resolve_city(self, destination: str) -> Any:
        """按用户输入返回规范城市，无法确认时抛出 LookupError。"""


class IntentCache(Protocol):
    """意图结果缓存的最小边界，当前由 SQLite provider_cache 实现。"""

    def get_cache(self, provider: str, key: str) -> Any | None:
        """读取未过期的缓存值。"""

    def set_cache(self, provider: str, key: str, value: Any, ttl_seconds: int) -> None:
        """保存带有效期的缓存值。"""


class PromptCacheTransport:
    """为明确支持该字段的模型服务附加稳定 prompt_cache_key。

    真实 KV Cache 位于模型供应商内部，本适配器只提供路由提示并保留原始用量字段。
    默认不启用，避免不兼容的 OpenAI-like 服务拒绝未知参数。
    """

    def __init__(
        self,
        cache_key: str,
        inner: ChatCompletionTransport | None = None,
    ) -> None:
        if not cache_key.strip():
            raise ValueError("prompt cache key must be non-empty")
        self._cache_key = cache_key.strip()
        self._inner = inner or UrllibChatCompletionTransport()

    async def complete(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """复制请求并添加缓存路由键，不修改调用方的原始负载。"""

        cached_payload = dict(payload)
        cached_payload["prompt_cache_key"] = self._cache_key
        response = await self._inner.complete(
            url=url,
            headers=headers,
            payload=cached_payload,
            timeout_seconds=timeout_seconds,
        )
        return cast(dict[str, Any], response)


@dataclass(frozen=True, slots=True)
class CommandGeneration:
    """模型命令与本轮实际读取的上下文清单。"""

    batch: TravelCommandBatch
    manifest: ContextManifest
    metadata: dict[str, Any]
    usage: AssistantTokenUsage
    source: Literal["intent_cache", "llm"] = "llm"


@dataclass(frozen=True, slots=True)
class _GeneratedCommands:
    """可在相同提示之间共享的模型结果，不携带某个会话的上下文引用。"""

    batch: TravelCommandBatch
    metadata: dict[str, Any]
    usage: AssistantTokenUsage


@dataclass(frozen=True, slots=True)
class CommandEffects:
    """命令执行后供对话策略使用的非持久信号。"""

    route_to_chat: bool = False
    confirmed: bool = False
    cancelled: bool = False
    validation_message: str | None = None
    memory_upserts: dict[MemorySlotName, str | list[str]] | None = None
    memory_deletes: frozenset[MemorySlotName] = frozenset()


@dataclass(slots=True)
class _CommandState:
    """命令批次处理期间使用的临时状态，处理完成前不会写入数据库。"""

    revision: int
    slots: TravelDialogueSlots
    metadata: dict[str, SlotMetadata]
    active_flow: AssistantFlow | None
    status: str
    changed: set[str]
    route_to_chat: bool = False
    confirmed: bool = False
    cancelled: bool = False
    validation_message: str | None = None
    memory_upserts: dict[MemorySlotName, str | list[str]] | None = None
    memory_deletes: set[MemorySlotName] | None = None


@dataclass(frozen=True, slots=True)
class DialogueDecision:
    """确定性 Flow 对一轮消息给出的下一步。"""

    state: TravelDialogueState
    reply: str
    missing_slots: tuple[str, ...] = ()
    planning_request: PlanningRequest | None = None


class TravelContextAssembler:
    """为意图识别构造分层、可观测且受预算约束的专属上下文。"""

    def __init__(self, max_chars: int = 5_000) -> None:
        self._builder = ContextManifestBuilder(max_chars=max_chars)

    def build(
        self,
        state: TravelDialogueState,
        conversation: ConversationContext | None,
        memories: tuple[TravelMemory, ...] = (),
    ) -> ContextManifest:
        """按任务事实、Skill、记忆、近期轮次和摘要的顺序应用预算。"""

        snapshot = {
            "active_flow": state.active_flow,
            "status": state.status,
            "slots": state.slots.model_dump(mode="json", exclude_none=True),
            "pending_slots": state.pending_slots,
        }
        recent = _render_recent(conversation, max_rounds=6, max_chars=650)
        summary = conversation.render_summary() if conversation is not None else ""
        skill = get_skill(state.active_flow)
        return self._builder.build(
            (
                ContextInput(
                    id="travel_dialogue_state",
                    source="travel_dialogue_store",
                    trust=ContextTrust.RUNTIME_STATE,
                    content=json.dumps(snapshot, ensure_ascii=False),
                    max_chars=900,
                ),
                ContextInput(
                    id="pending_question",
                    source="travel_dialogue_store",
                    trust=ContextTrust.RUNTIME_STATE,
                    content=state.last_question or "",
                    max_chars=160,
                ),
                ContextInput(
                    id="active_skill_contract",
                    source="travel_skill_registry",
                    trust=ContextTrust.HOST,
                    content=skill.context_contract() if skill else "",
                    max_chars=320,
                ),
                ContextInput(
                    id="confirmed_travel_memories",
                    source="travel_memory_store",
                    trust=ContextTrust.RECALLED_BACKGROUND,
                    content=_render_memories(memories),
                    max_chars=480,
                ),
                ContextInput(
                    id="recent_conversation",
                    source="conversation_store",
                    trust=ContextTrust.UNTRUSTED,
                    content=recent,
                    max_chars=1_600,
                ),
                ContextInput(
                    id="conversation_summary",
                    source="conversation_compaction",
                    trust=ContextTrust.RECALLED_BACKGROUND,
                    content=summary,
                    max_chars=600,
                ),
            )
        )


class TravelCommandGenerator:
    """受 Token、缓存和命令白名单约束的意图模型边界。"""

    CACHE_PROVIDER = "assistant_intent_v2"

    def __init__(
        self,
        model: ModelClient,
        timeout_seconds: float = 8,
        *,
        cache: IntentCache | None = None,
        cache_ttl_seconds: int = 3_600,
        cache_namespace: str = "default",
        max_context_chars: int = 5_000,
        max_input_tokens: int = 2_048,
        max_output_tokens: int = 512,
    ) -> None:
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._budget = ModelCallBudget(max_input_tokens, max_output_tokens)
        self._context = TravelContextAssembler(max_context_chars)
        self._cache = cache
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache_namespace = cache_namespace
        self._inflight: dict[str, asyncio.Task[_GeneratedCommands]] = {}
        self._inflight_lock = asyncio.Lock()

    async def generate(
        self,
        message: str,
        state: TravelDialogueState,
        conversation: ConversationContext | None,
        memories: tuple[TravelMemory, ...] = (),
        remaining_tokens: int | None = None,
    ) -> CommandGeneration:
        """优先读取精确结果缓存，未命中时合并并发模型请求。"""

        manifest = self._context.build(state, conversation, memories)
        messages = _intent_messages(message, manifest)
        cache_key = self._cache_key(messages)
        cached = self._read_cache(cache_key)
        if cached is not None:
            return CommandGeneration(
                cached,
                manifest,
                {"result_cache": "hit"},
                AssistantTokenUsage(),
                "intent_cache",
            )

        generated, owner = await self._shared_generate(
            cache_key, messages, remaining_tokens
        )
        return CommandGeneration(
            generated.batch,
            manifest,
            generated.metadata,
            generated.usage,
            "llm" if owner else "intent_cache",
        )

    async def _shared_generate(
        self,
        cache_key: str,
        messages: tuple[ModelMessage, ...],
        remaining_tokens: int | None,
    ) -> tuple[_GeneratedCommands, bool]:
        """相同提示只保留一个在途请求，等待方不会再次消费 Token。"""

        async with self._inflight_lock:
            task = self._inflight.get(cache_key)
            owner = task is None
            if task is None:
                task = asyncio.create_task(
                    self._generate_uncached(messages, remaining_tokens)
                )
                self._inflight[cache_key] = task
        try:
            result = await asyncio.shield(task)
            return result, owner
        finally:
            if owner:
                async with self._inflight_lock:
                    if self._inflight.get(cache_key) is task:
                        self._inflight.pop(cache_key, None)

    async def _generate_uncached(
        self,
        messages: tuple[ModelMessage, ...],
        remaining_tokens: int | None,
    ) -> _GeneratedCommands:
        """调用模型并最多修复一次，成功后写入去证据化结果缓存。"""

        responses = [await self._complete(messages, remaining_tokens)]
        try:
            batch = TravelCommandBatch.model_validate_json(responses[0].content)
        except ValidationError as error:
            used = _usage_from_responses(responses).total_tokens
            repair_limit = None if remaining_tokens is None else remaining_tokens - used
            repaired = await self._repair(responses[0].content, str(error), repair_limit)
            responses.append(repaired)
            try:
                batch = TravelCommandBatch.model_validate_json(repaired.content)
            except ValidationError as repair_error:
                raise AppError(
                    "intent_invalid_output", "暂时无法理解这条旅行需求，请换一种说法", 502
                ) from repair_error
        metadata = _generation_metadata(responses)
        result = _GeneratedCommands(batch, metadata, _usage_from_responses(responses))
        self._write_cache(self._cache_key(messages), batch)
        return result

    async def _repair(
        self, content: str, error: str, remaining_tokens: int | None
    ) -> ModelResponse:
        messages = (
            ModelMessage(role="system", content=_REPAIR_PROMPT),
            ModelMessage(
                role="user",
                content=json.dumps(
                    {"invalid_output": content[:1_000], "validation_error": error[:500]},
                    ensure_ascii=False,
                ),
            ),
        )
        return await self._complete(messages, remaining_tokens)

    async def _complete(
        self, messages: tuple[ModelMessage, ...], remaining_tokens: int | None
    ) -> ModelResponse:
        budget = self._call_budget(messages, remaining_tokens)
        try:
            return await asyncio.wait_for(
                complete_with_budget(
                    self._model,
                    messages,
                    budget=budget,
                    response_format="json_object",
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as error:
            raise AppError("intent_timeout", "意图识别超时，请重试", 504) from error
        except TokenBudgetExceededError as error:
            raise AppError(
                "intent_budget_exceeded", "当前会话的意图识别预算已经用完，请新建会话", 429
            ) from error
        except AppError:
            raise
        except Exception as error:
            raise AppError("intent_unavailable", "意图识别服务暂时不可用", 503) from error

    def _call_budget(
        self,
        messages: tuple[ModelMessage, ...],
        remaining_tokens: int | None,
    ) -> ModelCallBudget:
        """把单次上限与会话剩余额度取最小值，预留至少 64 Token 输出。"""

        if remaining_tokens is None:
            return self._budget
        estimated_input = estimate_message_tokens(messages)
        if remaining_tokens < estimated_input + 64:
            raise AppError(
                "intent_budget_exceeded", "当前会话的意图识别预算已经用完，请新建会话", 429
            )
        total = min(self._budget.max_total_tokens or remaining_tokens, remaining_tokens)
        return ModelCallBudget(
            max_input_tokens=min(self._budget.max_input_tokens, total - 64),
            max_output_tokens=min(self._budget.max_output_tokens, total - estimated_input),
            max_total_tokens=total,
        )

    def _cache_key(self, messages: tuple[ModelMessage, ...]) -> str:
        payload = {
            "schema": 2,
            "namespace": self._cache_namespace,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _read_cache(self, key: str) -> TravelCommandBatch | None:
        if self._cache is None:
            return None
        value = self._cache.get_cache(self.CACHE_PROVIDER, key)
        if not isinstance(value, dict) or value.get("schema") != 2:
            return None
        try:
            return TravelCommandBatch.model_validate(value.get("batch"))
        except ValidationError:
            return None

    def _write_cache(self, key: str, batch: TravelCommandBatch) -> None:
        if self._cache is None or self._cache_ttl_seconds <= 0:
            return
        payload = batch.model_dump(mode="json")
        for command in payload["commands"]:
            command.pop("evidence", None)
        self._cache.set_cache(
            self.CACHE_PROVIDER,
            key,
            {"schema": 2, "batch": payload},
            self._cache_ttl_seconds,
        )


def _usage_from_responses(responses: list[ModelResponse]) -> AssistantTokenUsage:
    """优先使用供应商计量，缺失时使用 OpenZLAgent 的保守估算。"""

    observed = aggregate_token_usage(*(item.raw.get("usage") for item in responses))
    if observed:
        input_tokens = observed.get("input_tokens", 0)
        output_tokens = observed.get("output_tokens", 0)
        total_tokens = max(observed.get("total_tokens", 0), input_tokens + output_tokens)
    else:
        budgets = [item.raw.get("token_budget") for item in responses]
        input_tokens = sum(
            int(item.get("estimated_input_tokens", 0))
            for item in budgets
            if isinstance(item, dict)
        )
        output_tokens = sum(
            int(item.get("estimated_output_tokens", 0))
            for item in budgets
            if isinstance(item, dict)
        )
        total_tokens = input_tokens + output_tokens
    cached_tokens = sum(_cached_input_tokens(item.raw) for item in responses)
    return AssistantTokenUsage(
        model_calls=len(responses),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=min(cached_tokens, input_tokens),
        total_tokens=total_tokens,
    )


def _generation_metadata(responses: list[ModelResponse]) -> dict[str, Any]:
    """合并修复调用的安全计量字段，不保留两份模型正文。"""

    metadata = dict(responses[-1].raw)
    usage = _usage_from_responses(responses)
    metadata["model_calls"] = usage.model_calls
    metadata["usage"] = usage.model_dump()
    metadata["prompt_cache"] = {
        "cached_input_tokens": usage.cached_input_tokens,
    }
    metadata["result_cache"] = "miss"
    return metadata


def _cached_input_tokens(raw: dict[str, Any]) -> int:
    """兼容常见 Chat Completions 与 Responses API 的缓存计量字段。"""

    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return 0
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        return max(0, int(details["cached_tokens"]))
    value = usage.get("cache_read_input_tokens")
    return max(0, value) if isinstance(value, int) else 0


def parse_fast_commands(
    message: str,
    state: TravelDialogueState,
    *,
    today: date | None = None,
) -> TravelCommandBatch | None:
    """只在整条消息可以无歧义消费时跳过模型。"""

    normalized = message.strip()
    compact = re.sub(r"[\s，,。.!！?？]", "", normalized)
    if compact in {"确认", "确定", "可以", "好", "好的"}:
        return TravelCommandBatch(commands=[ConfirmCommand(type="confirm")])
    if compact in {"取消", "算了", "不规划了", "停止"}:
        return TravelCommandBatch(commands=[CancelFlowCommand(type="cancel_flow")])
    if memory_command := _parse_fast_memory(normalized, compact):
        return TravelCommandBatch(commands=[memory_command])
    if not state.active_flow or not state.pending_slots:
        return None

    remaining = normalized
    commands: list[TravelCommand] = []
    for raw_name in state.pending_slots:
        if raw_name not in SLOT_NAMES:
            continue
        name = cast(SlotName, raw_name)
        parsed = _parse_pending_slot(name, remaining, today or date.today())
        if parsed is None:
            continue
        value, start, end = parsed
        commands.append(SetSlotCommand(type="set_slot", name=name, value=value))
        remaining = remaining[:start] + " " * (end - start) + remaining[end:]
    residue = re.sub(r"[\s，,。.!！?？、]", "", remaining)
    residue = re.sub(r"(?:大概|左右|差不多|计划|预算|玩|从|到|至|开始|结束|吧)", "", residue)
    return TravelCommandBatch(commands=commands) if commands and not residue else None


def validate_memory_commands(message: str, batch: TravelCommandBatch) -> None:
    """拒绝模型在没有用户明确授权时写入或删除长期记忆。"""

    has_remember = any(isinstance(item, RememberSlotCommand) for item in batch.commands)
    has_forget = any(isinstance(item, ForgetMemoryCommand) for item in batch.commands)
    if has_remember and not re.search(r"记住|以后都|下次默认|长期偏好", message):
        raise AppError("intent_invalid_output", "只有明确说“记住”时才会保存长期偏好", 422)
    if has_forget and not re.search(r"忘记|不要记|清除|删除", message):
        raise AppError("intent_invalid_output", "只有明确要求忘记时才会删除长期偏好", 422)


def apply_memory_defaults(
    state: TravelDialogueState, memories: tuple[TravelMemory, ...]
) -> TravelDialogueState:
    """把长期偏好作为低优先级默认值放入新会话，不覆盖现有槽位。"""

    slots = state.slots
    metadata = dict(state.slot_metadata)
    defaults = TravelDialogueSlots()
    for memory in memories:
        current = getattr(slots, memory.key)
        if memory.key in metadata or current != getattr(defaults, memory.key):
            continue
        try:
            slots = _replace_slot(slots, memory.key, memory.value)
        except ValidationError:
            continue
        metadata[memory.key] = SlotMetadata(source="memory", updated_turn=0)
    return state.model_copy(update={"slots": slots, "slot_metadata": metadata})


def apply_commands(
    state: TravelDialogueState,
    batch: TravelCommandBatch,
    city_resolver: CityResolver,
    *,
    now: datetime | None = None,
) -> tuple[TravelDialogueState, CommandEffects]:
    """校验命令并生成下一版本状态，不写数据库。"""

    current_time = now or datetime.now(timezone.utc)
    next_revision = state.revision + 1
    command_state = _CommandState(
        revision=next_revision,
        slots=state.slots.model_copy(deep=True),
        metadata=dict(state.slot_metadata),
        active_flow=state.active_flow,
        status=state.status,
        changed=set(),
        memory_upserts={},
        memory_deletes=set(),
    )
    for command in batch.commands:
        _apply_command(command_state, command, city_resolver)

    slots, metadata = _derive_dates(
        command_state.slots,
        command_state.metadata,
        command_state.changed,
        next_revision,
    )
    updated = state.model_copy(
        update={
            "revision": next_revision,
            "status": command_state.status,
            "active_flow": command_state.active_flow,
            "slots": slots,
            "slot_metadata": metadata,
            "updated_at": current_time,
        }
    )
    return updated, CommandEffects(
        route_to_chat=command_state.route_to_chat,
        confirmed=command_state.confirmed,
        cancelled=command_state.cancelled,
        validation_message=command_state.validation_message,
        memory_upserts=command_state.memory_upserts,
        memory_deletes=frozenset(command_state.memory_deletes or set()),
    )


def _apply_command(
    state: _CommandState,
    command: TravelCommand,
    city_resolver: CityResolver,
) -> None:
    """把一条受限命令应用到临时状态。"""

    if isinstance(command, StartFlowCommand):
        state.active_flow, state.status = command.flow, "collecting"
        return
    if isinstance(
        command,
        (SetSlotCommand, ClearSlotCommand, RememberSlotCommand, ForgetMemoryCommand),
    ):
        _apply_slot_command(state, command, city_resolver)
        return
    if isinstance(command, ConfirmCommand):
        state.confirmed = True
        return
    if isinstance(command, CancelFlowCommand):
        state.active_flow, state.status, state.cancelled = None, "closed", True
        return
    state.route_to_chat = True


def _apply_slot_command(
    state: _CommandState,
    command: SetSlotCommand | ClearSlotCommand | RememberSlotCommand | ForgetMemoryCommand,
    city_resolver: CityResolver,
) -> None:
    """集中处理槽位与长期偏好命令，主分发函数保持可读。"""

    if isinstance(command, SetSlotCommand):
        _set_slot(state, command, city_resolver)
        return
    if isinstance(command, ClearSlotCommand):
        _clear_slot(state, command.name)
        return
    if isinstance(command, RememberSlotCommand):
        _remember_slot(state, command, city_resolver)
        return
    _forget_memory(state, command.name)


def _clear_slot(state: _CommandState, name: SlotName) -> None:
    default = getattr(TravelDialogueSlots(), name)
    state.slots = _replace_slot(state.slots, name, default)
    state.metadata.pop(name, None)
    state.changed.add(name)


def _remember_slot(
    state: _CommandState,
    command: RememberSlotCommand,
    city_resolver: CityResolver,
) -> None:
    value = _set_slot(
        state,
        SetSlotCommand(type="set_slot", name=command.name, value=command.value),
        city_resolver,
    )
    if value is None or state.memory_upserts is None:
        return
    state.memory_upserts[command.name] = cast(str | list[str], value)
    if state.memory_deletes is not None:
        state.memory_deletes.discard(command.name)


def _forget_memory(state: _CommandState, name: MemorySlotName) -> None:
    if state.memory_deletes is not None:
        state.memory_deletes.add(name)
    if state.memory_upserts is not None:
        state.memory_upserts.pop(name, None)
    metadata = state.metadata.get(name)
    if metadata is None or metadata.source != "memory":
        return
    default = getattr(TravelDialogueSlots(), name)
    state.slots = _replace_slot(state.slots, name, default)
    state.metadata.pop(name, None)
    state.changed.add(name)


def _set_slot(
    state: _CommandState,
    command: SetSlotCommand,
    city_resolver: CityResolver,
) -> Any | None:
    """归一化并校验一个用户槽位，非法值不会覆盖原状态。"""

    try:
        value = _normalize_slot(command.name, command.value, city_resolver)
        state.slots = _replace_slot(state.slots, command.name, value)
    except (TypeError, ValueError, ValidationError) as error:
        state.validation_message = _slot_error_message(command.name, error)
        return None
    state.metadata[command.name] = SlotMetadata(
        source="user_explicit", updated_turn=state.revision
    )
    state.changed.add(command.name)
    return value


def _replace_slot(slots: TravelDialogueSlots, name: str, value: Any) -> TravelDialogueSlots:
    """通过 Pydantic 重建槽位，避免 model_copy 跳过字段范围校验。"""

    payload = slots.model_dump()
    payload[name] = value
    return TravelDialogueSlots.model_validate(payload)


def _slot_error_message(name: str, error: Exception) -> str:
    """将内部校验细节收敛为可直接展示的中文说明。"""

    if isinstance(error, ValueError) and not isinstance(error, ValidationError):
        return str(error)
    labels = {"days": "天数", "budget": "预算", "travelers": "人数"}
    return f"{labels.get(name, '旅行信息')}不在支持范围内，请重新填写"


def decide_next(state: TravelDialogueState, effects: CommandEffects) -> DialogueDecision:
    """根据结构化状态决定追问、等待推荐或启动规划。"""

    if effects.cancelled:
        decision = _decision(state, "已取消当前旅行需求。", ())
    elif effects.validation_message:
        decision = _decision(state, effects.validation_message, tuple(state.pending_slots))
    elif effects.route_to_chat:
        decision = DialogueDecision(
            state=state,
            reply="我目前可以帮你收集目的地偏好，或创建包含车票、酒店和路线的旅行计划。",
            missing_slots=tuple(state.pending_slots),
        )
    elif state.active_flow is None:
        decision = _decision(
            state,
            "你想先探索适合的目的地，还是已经有具体城市需要规划？",
            (),
        )
    elif state.active_flow == "destination_discovery":
        decision = _discovery_decision(state)
    else:
        decision = _planning_decision(state)
    return _with_memory_ack(decision, effects)


def _with_memory_ack(
    decision: DialogueDecision, effects: CommandEffects
) -> DialogueDecision:
    """把记忆变更确认附加到业务回复，避免产生第二次模型调用。"""

    labels = {
        "origin": "常用出发地",
        "preferences": "旅行偏好",
        "dietary_preferences": "饮食偏好",
        "pace": "旅行节奏",
        "hotel_level": "住宿档次",
        "transport_mode": "市内交通偏好",
    }
    saved = [labels[key] for key in (effects.memory_upserts or {})]
    deleted = [labels[key] for key in effects.memory_deletes]
    notices = []
    if saved:
        notices.append(f"已记住：{'、'.join(saved)}。")
    if deleted:
        notices.append(f"已忘记：{'、'.join(deleted)}。")
    if not notices:
        return decision
    return DialogueDecision(
        state=decision.state,
        reply="".join(notices) + decision.reply,
        missing_slots=decision.missing_slots,
        planning_request=decision.planning_request,
    )


def _discovery_decision(state: TravelDialogueState) -> DialogueDecision:
    slots = state.slots
    missing: list[str] = []
    if not (slots.destination_region or slots.destination_city or slots.origin):
        missing.append("destination_region")
    if slots.distance_preference and not slots.origin:
        missing.append("origin")
    if not slots.preferences:
        missing.append("preferences")
    if slots.days is None:
        missing.append("days")
    if slots.budget is None:
        missing.append("budget")
    if missing:
        return _decision(state, _question(missing), tuple(missing))
    ready = state.model_copy(
        update={
            "status": "recommendation_ready",
            "pending_slots": [],
            "last_question": None,
        }
    )
    return _decision(
        ready,
        "需求已经整理完成。当前版本不会凭空推荐城市；你可以补充一个具体城市继续生成完整计划。",
        (),
    )


def _planning_decision(state: TravelDialogueState) -> DialogueDecision:
    slots = state.slots
    missing = [
        name
        for name, value in (
            ("origin", slots.origin),
            ("destination_city", slots.destination_city),
            ("start_date", slots.start_date),
        )
        if value is None
    ]
    if slots.end_date is None and slots.days is None:
        missing.append("days")
    if slots.budget is None:
        missing.append("budget")
    if missing:
        return _decision(state, _question(missing), tuple(missing))
    assert slots.origin and slots.destination_city and slots.start_date and slots.end_date
    request = PlanningRequest(
        origin=slots.origin,
        destination=slots.destination_city,
        start_date=slots.start_date,
        end_date=slots.end_date,
        travelers=slots.travelers,
        budget=slots.budget or 0,
        pace=slots.pace,
        hotel_level=slots.hotel_level,
        transport_mode=slots.transport_mode,
        preferences=slots.preferences,
        dietary_preferences=slots.dietary_preferences,
        notes=slots.notes,
    )
    ready = state.model_copy(update={"pending_slots": [], "last_question": None})
    return DialogueDecision(
        state=ready,
        reply="旅行需求已经完整，正在查询车票、酒店、天气和当地信息。",
        planning_request=request,
    )


def _decision(
    state: TravelDialogueState, reply: str, missing: tuple[str, ...]
) -> DialogueDecision:
    updated = state.model_copy(
        update={"pending_slots": list(missing), "last_question": reply if missing else None}
    )
    return DialogueDecision(updated, reply, missing)


def _normalize_slot(name: str, value: Any, city_resolver: CityResolver) -> Any:
    if name in {"origin", "notes"}:
        return _text(value, name)
    if name == "destination_city":
        return _normalize_city(value, city_resolver)
    if name == "destination_region":
        return _normalize_region(_text(value, name))
    if name in {"start_date", "end_date"}:
        return _normalize_date(value)
    if name in {"days", "travelers"}:
        return int(value)
    if name == "budget":
        return float(value)
    if name in {"preferences", "dietary_preferences"}:
        return _normalize_list(value, name)
    return _normalize_choice(name, value)


def _normalize_city(value: Any, city_resolver: CityResolver) -> str:
    raw = _text(value, "destination_city")
    try:
        return str(city_resolver.resolve_city(raw).name)
    except (AttributeError, LookupError) as error:
        raise ValueError(f"暂时无法确认“{raw}”是具体城市，请换一个城市名称") from error


def _normalize_date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError("日期需要使用明确的年月日") from error


def _normalize_list(value: Any, name: str) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(_text(item, name) for item in values))


def _normalize_choice(name: str, value: Any) -> Any:
    allowed = {
        "distance_preference": {"near", "far"},
        "pace": {"轻松", "适中", "紧凑"},
        "hotel_level": {"经济", "舒适", "品质"},
        "transport_mode": {"auto", "walk", "driving", "transit", "realtime_driving"},
    }
    if name not in allowed or value in allowed[name]:
        return value
    raise ValueError(f"{name} 的取值不受支持")


def _derive_dates(
    slots: TravelDialogueSlots,
    metadata: dict[str, SlotMetadata],
    changed: set[str],
    revision: int,
) -> tuple[TravelDialogueSlots, dict[str, SlotMetadata]]:
    start_date = slots.start_date
    if start_date is None:
        return slots, metadata
    if slots.days is not None and ("days" in changed or slots.end_date is None):
        computed_end = start_date + timedelta(days=slots.days - 1)
        slots = _replace_slot(slots, "end_date", computed_end)
        metadata["end_date"] = SlotMetadata(source="deterministic", updated_turn=revision)
        return slots, metadata
    explicit_end = slots.end_date
    should_derive_days = explicit_end is not None and (
        "end_date" in changed or slots.days is None or "start_date" in changed
    )
    if should_derive_days and explicit_end is not None:
        days = (explicit_end - start_date).days + 1
        if not 1 <= days <= 7:
            raise AppError("dialogue_validation_error", "行程日期必须为 1 到 7 天", 422)
        slots = _replace_slot(slots, "days", days)
        metadata["days"] = SlotMetadata(source="deterministic", updated_turn=revision)
    return slots, metadata


def _question(missing: list[str]) -> str:
    questions = {
        ("days", "budget"): "计划玩几天？大概预算是多少？",
        ("origin", "destination_city"): "你从哪里出发，准备去哪个具体城市？",
        ("start_date", "days"): "计划哪天出发，一共玩几天？",
    }
    if len(missing) >= 2 and (question := questions.get((missing[0], missing[1]))):
        return question
    labels = {
        "origin": "从哪里出发",
        "destination_region": "希望去哪个地区，或者从哪里出发寻找周边目的地",
        "destination_city": "准备去哪个具体城市",
        "start_date": "计划哪天出发",
        "end_date": "计划哪天结束",
        "days": "计划玩几天",
        "budget": "大概预算是多少",
        "preferences": "更喜欢历史、自然、美食还是亲子体验",
    }
    return "，".join(labels.get(item, "还需要补充旅行信息") for item in missing[:2]) + "？"


def _parse_fast_memory(
    message: str, compact: str
) -> RememberSlotCommand | ForgetMemoryCommand | None:
    """确定性处理少量明确记忆语句，复杂表达仍交给受限模型。"""

    aliases: dict[MemorySlotName, tuple[str, ...]] = {
        "origin": ("出发地", "常用出发地"),
        "preferences": ("旅行偏好", "游玩偏好"),
        "dietary_preferences": ("饮食偏好", "口味偏好"),
        "pace": ("旅行节奏", "游玩节奏"),
        "hotel_level": ("住宿档次", "酒店偏好"),
        "transport_mode": ("交通偏好", "市内交通偏好"),
    }
    for key, names in aliases.items():
        if compact in {f"忘记我的{name}" for name in names} | {
            f"清除我的{name}" for name in names
        }:
            return ForgetMemoryCommand(type="forget_memory", name=key)

    origin = re.fullmatch(r"记住我(?:通常|默认)?从(?P<value>[^，。]{1,20})出发", compact)
    if origin:
        return RememberSlotCommand(
            type="remember_slot", name="origin", value=origin.group("value")
        )
    preference = re.fullmatch(r"记住我(?:喜欢|偏好)(?P<value>[^。]{1,80})", message.strip())
    if preference:
        values = [
            item.strip()
            for item in re.split(r"[、,，]|和", preference.group("value"))
            if item.strip()
        ]
        return RememberSlotCommand(
            type="remember_slot", name="preferences", value=values
        )
    return None


def _parse_pending_slot(
    name: str, content: str, today: date
) -> tuple[Any, int, int] | None:
    patterns = {
        "days": re.compile(r"(?P<value>[1-7一二两三四五六七])\s*天"),
        "budget": re.compile(
            r"(?:预算\s*)?(?P<value>\d+(?:\.\d+)?|[一二两三四五六七八九])\s*"
            r"(?P<unit>万|千|k|K)?\s*元?"
        ),
        "travelers": re.compile(r"(?P<value>\d+|[一二两三四五六七八九])\s*(?:人|位)"),
        "start_date": re.compile(
            r"(?:(?P<year>\d{4})[-/年])?(?P<month>\d{1,2})(?:[-/月])(?P<day>\d{1,2})日?"
        ),
        "end_date": re.compile(
            r"(?:(?P<year>\d{4})[-/年])?(?P<month>\d{1,2})(?:[-/月])(?P<day>\d{1,2})日?"
        ),
    }
    pattern = patterns.get(name)
    if pattern is None or (match := pattern.search(content)) is None:
        return None
    if name == "budget":
        number = _number(match.group("value"))
        factor = {"万": 10_000, "千": 1_000, "k": 1_000, "K": 1_000}.get(
            match.group("unit"), 1
        )
        value: Any = number * factor
    elif name in {"days", "travelers"}:
        value = int(_number(match.group("value")))
    else:
        try:
            value = date(
                int(match.group("year") or today.year),
                int(match.group("month")),
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            return None
    return value, match.start(), match.end()


def _number(value: str) -> float:
    chinese = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    return float(chinese[value]) if value in chinese else float(value)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空文本")
    return value.strip()


def _normalize_region(value: str) -> str:
    normalized = _REGION_ALIASES.get(value, value)
    if normalized not in _REGION_NAMES:
        raise ValueError(f"暂时无法确认“{value}”是省级地区")
    return normalized


def _render_recent(
    context: ConversationContext | None, *, max_rounds: int, max_chars: int
) -> str:
    if context is None:
        return ""
    selected: list[str] = []
    used = 0
    for turn in reversed(context.recent_turns[-max_rounds:]):
        block = (
            f'<turn sequence="{turn.sequence}"><user>{escape(turn.user_content)}</user>'
            f"<assistant>{escape(turn.assistant_content)}</assistant></turn>"
        )
        if used + len(block) > max_chars:
            continue
        selected.append(block)
        used += len(block)
    return "\n".join(reversed(selected))


def _render_memories(memories: tuple[TravelMemory, ...]) -> str:
    """只渲染稳定偏好及版本，不携带来源会话和时间戳。"""

    payload = [
        {"key": item.key, "value": item.value, "version": item.version}
        for item in memories
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload else ""


def _intent_messages(message: str, manifest: ContextManifest) -> tuple[ModelMessage, ...]:
    payload = {"current_message": message, "context": manifest.render()}
    return (
        ModelMessage(role="system", content=_INTENT_PROMPT),
        ModelMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    )


_INTENT_PROMPT = """You are a travel dialogue command generator, not an executor.
Return exactly one JSON object: {"commands": [...]} and no prose.
Allowed commands:
- {"type":"start_flow","flow":"destination_discovery|trip_planning"}
- {"type":"set_slot","name":"<allowed slot>","value":...,"evidence":"exact user text"}
- {"type":"clear_slot","name":"<allowed slot>"}
- {"type":"remember_slot","name":"<memory slot>","value":...,"evidence":"exact user text"}
- {"type":"forget_memory","name":"<memory slot>"}
- {"type":"confirm"} | {"type":"cancel_flow"} | {"type":"route_to_chat"}
Allowed slots: origin, destination_region, destination_city, start_date, end_date,
days, budget, travelers, preferences, dietary_preferences, distance_preference,
pace, hotel_level, transport_mode, notes.
Memory slots: origin, preferences, dietary_preferences, pace, hotel_level,
transport_mode. Emit remember_slot only when the current message explicitly asks
to remember a stable preference. Emit forget_memory only for an explicit forget request.
Use destination_discovery for recommendations or an area without a concrete city.
Use trip_planning when the user asks to plan a concrete city trip. Province-level
places such as 广西 are destination_region, not destination_city. Dates must be
YYYY-MM-DD. distance_preference is near or far. Use only explicit user information.
Preserve omitted slots, focus on the last message, and never call tools or invent facts.
"""

_REPAIR_PROMPT = """Repair an invalid travel command JSON object. Return only
{"commands":[...]} using the exact command and slot schema from the validation error.
Do not add facts, prose, tools, SQL, locations, prices, routes, or provider data."""

_REGION_ALIASES = {
    "北京": "北京市", "天津": "天津市", "上海": "上海市", "重庆": "重庆市",
    "河北": "河北省", "山西": "山西省", "辽宁": "辽宁省", "吉林": "吉林省",
    "黑龙江": "黑龙江省", "江苏": "江苏省", "浙江": "浙江省", "安徽": "安徽省",
    "福建": "福建省", "江西": "江西省", "山东": "山东省", "河南": "河南省",
    "湖北": "湖北省", "湖南": "湖南省", "广东": "广东省", "海南": "海南省",
    "四川": "四川省", "贵州": "贵州省", "云南": "云南省", "陕西": "陕西省",
    "甘肃": "甘肃省", "青海": "青海省", "台湾": "台湾省",
    "内蒙古": "内蒙古自治区", "广西": "广西壮族自治区",
    "西藏": "西藏自治区", "宁夏": "宁夏回族自治区",
    "新疆": "新疆维吾尔自治区", "香港": "香港特别行政区",
    "澳门": "澳门特别行政区",
}
_REGION_NAMES = frozenset(_REGION_ALIASES.values())
