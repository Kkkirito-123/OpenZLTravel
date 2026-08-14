"""专属上下文、受限命令与确定性旅行 Flow 测试。"""

import asyncio
from datetime import date, datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError
from re_zlagent.harness.conversation import ConversationContext, ConversationTurn
from re_zlagent.harness.model import ModelMessage, ModelResponse

from app.dialogue import (
    CancelFlowCommand,
    ConfirmCommand,
    ForgetMemoryCommand,
    PromptCacheTransport,
    RememberSlotCommand,
    SetSlotCommand,
    StartFlowCommand,
    TravelCommandBatch,
    TravelCommandGenerator,
    TravelContextAssembler,
    apply_commands,
    apply_memory_defaults,
    decide_next,
    parse_fast_commands,
    validate_memory_commands,
)
from app.errors import AppError
from app.models import City, TravelDialogueState, TravelMemory
from app.skills import list_skill_views

SESSION_ID = UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class FakeCityResolver:
    """只接受测试中明确列出的真实城市。"""

    def resolve_city(self, destination: str) -> City:
        if destination not in {"上海", "杭州"}:
            raise LookupError(destination)
        return City(name=f"{destination}市")


class SequenceModel:
    """按顺序返回模型响应，便于验证一次修复。"""

    def __init__(self, *responses: str, delay: float = 0) -> None:
        self.responses = list(responses)
        self.delay = delay
        self.calls: list[tuple[ModelMessage, ...]] = []

    async def complete(self, messages: tuple[ModelMessage, ...]) -> ModelResponse:
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        return ModelResponse(self.responses.pop(0))


class UsageModel:
    """返回供应商缓存计量，验证 KV 命中只作为观测值处理。"""

    async def complete(self, messages: tuple[ModelMessage, ...]) -> ModelResponse:
        return ModelResponse(
            '{"commands":[{"type":"start_flow","flow":"trip_planning"}]}',
            {
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 30,
                    "total_tokens": 1230,
                    "prompt_tokens_details": {"cached_tokens": 1024},
                }
            },
        )


class DictCache:
    """测试用同步缓存，行为与仓库的 provider_cache 边界一致。"""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], object] = {}

    def get_cache(self, provider: str, key: str) -> object | None:
        return self.values.get((provider, key))

    def set_cache(
        self, provider: str, key: str, value: object, ttl_seconds: int
    ) -> None:
        assert ttl_seconds > 0
        self.values[(provider, key)] = value


class CaptureTransport:
    """记录模型请求，验证可选 prompt_cache_key 不污染原始负载。"""

    def __init__(self) -> None:
        self.payload: dict[str, object] = {}

    async def complete(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        self.payload = dict(payload)
        return {"choices": [{"message": {"content": "{}"}}]}


def dialogue_state(
    *, active_flow: str | None = None, pending_slots: list[str] | None = None
) -> TravelDialogueState:
    """构造没有供应商事实的对话状态。"""

    return TravelDialogueState(
        session_id=SESSION_ID,
        active_flow=active_flow,
        pending_slots=pending_slots or [],
        created_at=NOW,
        updated_at=NOW,
    )


def test_skill_registry_is_small_and_has_deterministic_effects() -> None:
    skills = list_skill_views()

    assert [skill.id for skill in skills] == [
        "destination_discovery",
        "trip_planning",
    ]
    assert skills[0].effect == "collect_requirements"
    assert skills[1].effect == "start_planning"


def test_old_dialogue_json_gets_new_telemetry_defaults() -> None:
    payload = dialogue_state().model_dump(mode="json")
    payload.pop("token_usage")

    restored = TravelDialogueState.model_validate(payload)

    assert restored.token_usage.total_tokens == 0


def test_fast_parser_consumes_only_unambiguous_pending_values() -> None:
    state = dialogue_state(active_flow="trip_planning", pending_slots=["days", "budget"])

    batch = parse_fast_commands("三天，预算两千元", state, today=date(2026, 8, 14))

    assert batch is not None
    updated, _ = apply_commands(state, batch, FakeCityResolver(), now=NOW)
    assert updated.slots.days == 3
    assert updated.slots.budget == 2000
    assert updated.slots.end_date is None


def test_fast_parser_defers_to_llm_when_semantics_remain() -> None:
    state = dialogue_state(active_flow="trip_planning", pending_slots=["days", "budget"])

    assert parse_fast_commands("三天，两千元，还想看历史建筑", state) is None


def test_fast_parser_handles_confirm_and_cancel() -> None:
    state = dialogue_state()

    confirmed = parse_fast_commands("好的", state)
    cancelled = parse_fast_commands("算了", state)

    assert confirmed is not None
    assert isinstance(confirmed.commands[0], ConfirmCommand)
    assert cancelled is not None
    assert isinstance(cancelled.commands[0], CancelFlowCommand)


def test_fast_parser_requires_explicit_memory_language() -> None:
    state = dialogue_state()

    remembered = parse_fast_commands("记住我从杭州出发", state)
    forgotten = parse_fast_commands("忘记我的出发地", state)

    assert remembered is not None
    assert isinstance(remembered.commands[0], RememberSlotCommand)
    assert forgotten is not None
    assert isinstance(forgotten.commands[0], ForgetMemoryCommand)


def test_model_cannot_write_memory_without_explicit_authorization() -> None:
    batch = TravelCommandBatch(
        commands=[
            RememberSlotCommand(type="remember_slot", name="pace", value="轻松")
        ]
    )

    with pytest.raises(AppError) as caught:
        validate_memory_commands("我喜欢轻松一点", batch)

    assert caught.value.code == "intent_invalid_output"


def test_fast_parser_handles_explicit_date_range_and_rejects_invalid_date() -> None:
    state = dialogue_state(
        active_flow="trip_planning", pending_slots=["start_date", "end_date"]
    )

    batch = parse_fast_commands(
        "2026-09-01 到 2026-09-03", state, today=date(2026, 8, 14)
    )

    assert batch is not None
    updated, _ = apply_commands(state, batch, FakeCityResolver(), now=NOW)
    assert updated.slots.start_date == date(2026, 9, 1)
    assert updated.slots.end_date == date(2026, 9, 3)
    assert updated.slots.days == 3
    assert parse_fast_commands("2026-02-30", state) is None


def test_command_schema_rejects_unknown_command_and_slot() -> None:
    with pytest.raises(ValidationError):
        TravelCommandBatch.model_validate(
            {"commands": [{"type": "call_tool", "name": "amap"}]}
        )
    with pytest.raises(ValidationError):
        TravelCommandBatch.model_validate(
            {"commands": [{"type": "set_slot", "name": "poi", "value": "外滩"}]}
        )


def test_context_contains_state_and_complete_recent_rounds_only() -> None:
    turns = tuple(
        ConversationTurn(
            session_id=str(SESSION_ID),
            sequence=index,
            user_content=f"用户消息 {index}",
            assistant_content=f"助手回复 {index}",
        )
        for index in range(1, 8)
    )
    context = ConversationContext(
        session_id=str(SESSION_ID),
        summary="用户偏好历史景观",
        recent_turns=turns,
        stored_turn_count=7,
    )
    state = dialogue_state(active_flow="destination_discovery", pending_slots=["days"])
    state = state.model_copy(update={"last_question": "计划玩几天？"})

    manifest = TravelContextAssembler().build(state, context)
    rendered = manifest.render()

    assert manifest.used_chars <= 5_000
    assert "travel_dialogue_state" in rendered
    assert "计划玩几天" in rendered
    assert 'sequence="1"' not in rendered
    assert 'sequence="7"' in rendered
    assert "active_skill_contract" in rendered
    assert "destination_discovery" in rendered
    assert "tool" not in rendered.lower()
    assert "车票" not in rendered
    assert rendered.count("<turn ") == rendered.count("</turn>")


@pytest.mark.asyncio
async def test_command_generator_repairs_invalid_output_once() -> None:
    model = SequenceModel(
        '{"commands":[{"type":"unknown"}]}',
        '{"commands":[{"type":"start_flow","flow":"trip_planning"}]}',
    )
    generator = TravelCommandGenerator(model)

    generated = await generator.generate("帮我规划上海", dialogue_state(), None)

    assert isinstance(generated.batch.commands[0], StartFlowCommand)
    assert len(model.calls) == 2
    assert generated.metadata["token_budget"]["max_output_tokens"] == 512


@pytest.mark.asyncio
async def test_command_generator_timeout_uses_stable_error() -> None:
    generator = TravelCommandGenerator(SequenceModel("{}", delay=0.05), 0.001)

    with pytest.raises(AppError) as caught:
        await generator.generate("帮我规划上海", dialogue_state(), None)

    assert caught.value.code == "intent_timeout"


@pytest.mark.asyncio
async def test_exact_intent_cache_avoids_second_model_call() -> None:
    cache = DictCache()
    model = SequenceModel('{"commands":[{"type":"start_flow","flow":"trip_planning"}]}')
    generator = TravelCommandGenerator(model, cache=cache)

    first = await generator.generate("规划上海", dialogue_state(), None)
    second = await generator.generate("规划上海", dialogue_state(), None)

    assert first.source == "llm"
    assert second.source == "intent_cache"
    assert second.usage.total_tokens == 0
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_provider_cached_tokens_are_recorded_without_changing_total() -> None:
    generated = await TravelCommandGenerator(UsageModel()).generate(
        "规划上海", dialogue_state(), None
    )

    assert generated.usage.input_tokens == 1200
    assert generated.usage.cached_input_tokens == 1024
    assert generated.usage.total_tokens == 1230


@pytest.mark.asyncio
async def test_same_intent_inflight_request_is_merged() -> None:
    model = SequenceModel(
        '{"commands":[{"type":"start_flow","flow":"trip_planning"}]}',
        delay=0.02,
    )
    generator = TravelCommandGenerator(model)

    first, second = await asyncio.gather(
        generator.generate("规划上海", dialogue_state(), None),
        generator.generate("规划上海", dialogue_state(), None),
    )

    assert len(model.calls) == 1
    assert {first.source, second.source} == {"llm", "intent_cache"}


@pytest.mark.asyncio
async def test_prompt_cache_transport_adds_key_without_mutating_payload() -> None:
    inner = CaptureTransport()
    transport = PromptCacheTransport("openzltravel:intent:v2", inner)  # type: ignore[arg-type]
    payload: dict[str, object] = {"model": "gpt-test", "messages": []}

    await transport.complete(
        url="https://api.example.test/chat/completions",
        headers={},
        payload=payload,
        timeout_seconds=1,
    )

    assert "prompt_cache_key" not in payload
    assert inner.payload["prompt_cache_key"] == "openzltravel:intent:v2"


def test_memory_defaults_are_lower_priority_than_explicit_slots() -> None:
    memory = TravelMemory(
        key="origin",
        value="杭州",
        version=1,
        source_session_id=SESSION_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    state = apply_memory_defaults(dialogue_state(), (memory,))
    batch = TravelCommandBatch(
        commands=[SetSlotCommand(type="set_slot", name="origin", value="上海")]
    )

    updated, _ = apply_commands(state, batch, FakeCityResolver(), now=NOW)

    assert updated.slots.origin == "上海"
    assert updated.slot_metadata["origin"].source == "user_explicit"


def test_guangxi_history_discovery_reaches_recommendation_ready() -> None:
    state = dialogue_state()
    first = TravelCommandBatch(
        commands=[
            StartFlowCommand(type="start_flow", flow="destination_discovery"),
            SetSlotCommand(
                type="set_slot", name="destination_region", value="广西"
            ),
            SetSlotCommand(type="set_slot", name="preferences", value=["历史景点"]),
        ]
    )
    state, effects = apply_commands(state, first, FakeCityResolver(), now=NOW)
    decision = decide_next(state, effects)
    assert decision.missing_slots == ("days", "budget")

    second = parse_fast_commands("三天，预算两千元", decision.state)
    assert second is not None
    state, effects = apply_commands(decision.state, second, FakeCityResolver(), now=NOW)
    decision = decide_next(state, effects)

    assert decision.state.status == "recommendation_ready"
    assert decision.planning_request is None
    assert decision.state.slots.destination_region == "广西壮族自治区"


def test_nearby_discovery_requires_origin() -> None:
    batch = TravelCommandBatch(
        commands=[
            StartFlowCommand(type="start_flow", flow="destination_discovery"),
            SetSlotCommand(type="set_slot", name="distance_preference", value="near"),
            SetSlotCommand(type="set_slot", name="preferences", value=["历史景观"]),
        ]
    )

    state, effects = apply_commands(dialogue_state(), batch, FakeCityResolver(), now=NOW)
    decision = decide_next(state, effects)

    assert "origin" in decision.missing_slots
    assert "出发" in decision.reply


def test_concrete_trip_builds_existing_planning_request() -> None:
    batch = TravelCommandBatch(
        commands=[
            StartFlowCommand(type="start_flow", flow="trip_planning"),
            SetSlotCommand(type="set_slot", name="origin", value="杭州"),
            SetSlotCommand(type="set_slot", name="destination_city", value="上海"),
            SetSlotCommand(type="set_slot", name="start_date", value="2026-09-01"),
            SetSlotCommand(type="set_slot", name="days", value=3),
            SetSlotCommand(type="set_slot", name="budget", value=2000),
        ]
    )

    state, effects = apply_commands(dialogue_state(), batch, FakeCityResolver(), now=NOW)
    decision = decide_next(state, effects)

    assert decision.planning_request is not None
    assert decision.planning_request.origin == "杭州"
    assert decision.planning_request.destination == "上海市"
    assert decision.planning_request.end_date == date(2026, 9, 3)


def test_unknown_city_does_not_replace_existing_slot() -> None:
    batch = TravelCommandBatch(
        commands=[
            StartFlowCommand(type="start_flow", flow="trip_planning"),
            SetSlotCommand(type="set_slot", name="destination_city", value="不存在市"),
        ]
    )

    state, effects = apply_commands(dialogue_state(), batch, FakeCityResolver(), now=NOW)
    decision = decide_next(state, effects)

    assert state.slots.destination_city is None
    assert "无法确认" in decision.reply
