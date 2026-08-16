"""旅行意图的受限模型调用边界。

本文件只负责把专属上下文交给模型、校验命令 JSON、记录真实用量和复用相同请求。
模型不能访问工具或供应商事实，Flow 决策仍由 ``dialogue_flow`` 的确定性代码完成。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

from pydantic import ValidationError
from re_zlagent.harness.context import ContextManifest  # type: ignore[import-untyped]
from re_zlagent.harness.conversation import (  # type: ignore[import-untyped]
    ConversationContext,
)
from re_zlagent.harness.model import (  # type: ignore[import-untyped]
    ModelClient,
    ModelMessage,
    ModelResponse,
    aggregate_token_usage,
    estimate_message_tokens,
    estimate_text_tokens,
)
from re_zlagent.harness.model.openai_compatible import (  # type: ignore[import-untyped]
    ChatCompletionTransport,
    UrllibChatCompletionTransport,
)

from app.dialogue_commands import (
    CommandGeneration,
    GeneratedCommands,
    IntentCache,
    TravelCommandBatch,
)
from app.dialogue_context import TravelContextAssembler
from app.errors import AppError
from app.models import AssistantTokenUsage, TravelDialogueState, TravelMemory


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


class TravelCommandGenerator:
    """受缓存、超时和命令白名单约束的意图模型边界。"""

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
    ) -> None:
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._context = TravelContextAssembler(max_context_chars)
        self._cache = cache
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache_namespace = cache_namespace
        self._inflight: dict[str, asyncio.Task[GeneratedCommands]] = {}
        self._inflight_lock = asyncio.Lock()

    async def generate(
        self,
        message: str,
        state: TravelDialogueState,
        conversation: ConversationContext | None,
        memories: tuple[TravelMemory, ...] = (),
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

        generated, owner = await self._shared_generate(cache_key, messages)
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
    ) -> tuple[GeneratedCommands, bool]:
        """相同提示只保留一个在途请求，等待方不会再次消费 Token。"""

        async with self._inflight_lock:
            task = self._inflight.get(cache_key)
            owner = task is None
            if task is None:
                task = asyncio.create_task(self._generate_uncached(messages))
                self._inflight[cache_key] = task
                task.add_done_callback(
                    lambda completed: self._discard_inflight(cache_key, completed)
                )
        # 页面断开或单次请求取消时，不能中断其他等待相同意图的用户请求。
        # 清理动作由任务完成回调执行，避免先取消的创建者提前删除合并记录。
        result = await asyncio.shield(task)
        return result, owner

    def _discard_inflight(
        self,
        cache_key: str,
        completed: asyncio.Future[GeneratedCommands],
    ) -> None:
        """只由已完成任务清理在途记录，并消费无人等待时的异常。"""

        if self._inflight.get(cache_key) is completed:
            self._inflight.pop(cache_key, None)
        if not completed.cancelled():
            with contextlib.suppress(Exception):
                completed.exception()

    async def _generate_uncached(
        self,
        messages: tuple[ModelMessage, ...],
    ) -> GeneratedCommands:
        """调用模型并最多修复一次，成功后写入去证据化结果缓存。"""

        responses = [await self._complete(messages)]
        try:
            batch = TravelCommandBatch.model_validate_json(responses[0].content)
        except ValidationError as error:
            repaired = await self._repair(responses[0].content, str(error))
            responses.append(repaired)
            try:
                batch = TravelCommandBatch.model_validate_json(repaired.content)
            except ValidationError as repair_error:
                raise AppError(
                    "intent_invalid_output", "暂时无法理解这条旅行需求，请换一种说法", 502
                ) from repair_error
        metadata = _generation_metadata(responses)
        result = GeneratedCommands(batch, metadata, _usage_from_responses(responses))
        self._write_cache(self._cache_key(messages), batch)
        return result

    async def _repair(self, content: str, error: str) -> ModelResponse:
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
        return await self._complete(messages)

    async def _complete(self, messages: tuple[ModelMessage, ...]) -> ModelResponse:
        """不限制生成 Token，只约束超时并记录实际或估算用量。"""

        configurable = getattr(self._model, "complete_with_options", None)
        if callable(configurable):
            completion = cast(Callable[..., Awaitable[ModelResponse]], configurable)(
                messages, response_format="json_object"
            )
        else:
            completion = self._model.complete(messages)
        try:
            response = await asyncio.wait_for(completion, timeout=self._timeout_seconds)
            return _attach_usage_estimate(response, messages)
        except TimeoutError as error:
            raise AppError("intent_timeout", "意图识别超时，请重试", 504) from error
        except AppError:
            raise
        except Exception as error:
            raise AppError("intent_unavailable", "意图识别服务暂时不可用", 503) from error

    def _cache_key(self, messages: tuple[ModelMessage, ...]) -> str:
        payload = {
            "schema": 2,
            "namespace": self._cache_namespace,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
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


def _attach_usage_estimate(
    response: ModelResponse, messages: tuple[ModelMessage, ...]
) -> ModelResponse:
    """供应商未返回 usage 时附加估算值，不把估算当成调用上限。"""

    if aggregate_token_usage(response.raw.get("usage")):
        return response
    raw = dict(response.raw)
    input_tokens = estimate_message_tokens(messages)
    output_tokens = estimate_text_tokens(response.content)
    raw["usage_estimate"] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    return ModelResponse(content=response.content, raw=raw)


def _usage_from_responses(responses: list[ModelResponse]) -> AssistantTokenUsage:
    """优先使用供应商计量，缺失时使用 OpenZLAgent 的保守估算。"""

    usages = [
        aggregate_token_usage(item.raw.get("usage"))
        or aggregate_token_usage(item.raw.get("usage_estimate"))
        for item in responses
    ]
    input_tokens = sum(item.get("input_tokens", 0) for item in usages)
    output_tokens = sum(item.get("output_tokens", 0) for item in usages)
    total_tokens = sum(
        max(
            item.get("total_tokens", 0),
            item.get("input_tokens", 0) + item.get("output_tokens", 0),
        )
        for item in usages
    )
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
    metadata["prompt_cache"] = {"cached_input_tokens": usage.cached_input_tokens}
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
