"""旅行助手服务的状态、幂等、并发和恢复测试。"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from re_zlagent.harness.conversation import (
    ConversationContext,
    ConversationManager,
    SqliteConversationStore,
)

from app.assistant import TravelAssistantService
from app.dialogue import TravelCommandGenerator
from app.errors import AppError
from app.main import app, get_assistant_service
from app.models import AssistantMessageRequest, PlanningRequest, PlanningSession
from tests.sqlite_repository import SqliteTripRepository
from tests.test_dialogue import FakeCityResolver, SequenceModel

PLANNING_ID = UUID("22222222-2222-2222-2222-222222222222")


class FakePlanningRuntime:
    """记录规划启动次数，不执行任何供应商查询。"""

    def __init__(self) -> None:
        self.calls: list[tuple[PlanningRequest, str | None]] = []

    def start(
        self,
        request: PlanningRequest,
        idempotency_key: str | None = None,
        visitor_id: UUID | None = None,
    ) -> PlanningSession:
        del visitor_id
        self.calls.append((request, idempotency_key))
        now = datetime.now(timezone.utc)
        return PlanningSession(
            session_id=PLANNING_ID,
            request=request,
            created_at=now,
            updated_at=now,
        )


class FailingConversationManager:
    """模拟会话轮次或摘要写入失败。"""

    async def prepare(self, session_id: str) -> ConversationContext:
        return ConversationContext(session_id=session_id)

    async def record_turn(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("conversation store unavailable")


class AssistantEnvironment:
    """为一项测试组合真实 SQLite 状态与会话存储。"""

    def __init__(
        self,
        database: Path,
        *responses: str,
        delay: float = 0,
        configured: bool = True,
    ) -> None:
        self.repository = SqliteTripRepository(str(database))
        self.conversation_store = SqliteConversationStore(database)
        self.conversations = ConversationManager(self.conversation_store)
        self.runtime = FakePlanningRuntime()
        self.model = SequenceModel(*responses, delay=delay)
        generator = TravelCommandGenerator(self.model) if configured else None
        self.service = TravelAssistantService(
            self.repository,
            self.conversations,
            FakeCityResolver(),
            self.runtime,
            generator,
        )

    def close(self) -> None:
        self.conversation_store.close()


FULL_TRIP_COMMAND = """{
  "commands": [
    {"type":"start_flow","flow":"trip_planning"},
    {"type":"set_slot","name":"origin","value":"杭州"},
    {"type":"set_slot","name":"destination_city","value":"上海"},
    {"type":"set_slot","name":"start_date","value":"2026-09-01"},
    {"type":"set_slot","name":"days","value":3},
    {"type":"set_slot","name":"budget","value":2000}
  ]
}"""

DISCOVERY_COMMAND = """{
  "commands": [
    {"type":"start_flow","flow":"destination_discovery"},
    {"type":"set_slot","name":"destination_region","value":"广西"},
    {"type":"set_slot","name":"preferences","value":["历史景点"]}
  ]
}"""


@pytest.mark.asyncio
async def test_complete_message_starts_one_planning_session(tmp_path: Path) -> None:
    environment = AssistantEnvironment(tmp_path / "assistant.sqlite3", FULL_TRIP_COMMAND)
    try:
        session = environment.service.create()
        request = AssistantMessageRequest(message_id=uuid4(), content="杭州去上海玩三天")

        response = await environment.service.send(session.state.session_id, request)
        restored = await environment.service.get(session.state.session_id)

        assert response.state.status == "planning_started"
        assert response.planning_session_id == PLANNING_ID
        assert response.missing_slots == []
        assert len(environment.runtime.calls) == 1
        assert environment.runtime.calls[0][1] == (f"assistant:{session.state.session_id}:1")
        assert restored.state.planning_session_id == PLANNING_ID
        assert restored.turns[0].user_content == request.content
        metadata = environment.conversation_store.list_turns(str(session.state.session_id))[
            0
        ].metadata
        segments = metadata["context_ref"]["manifest"]["segments"]
        assert all("content" not in segment for segment in segments)
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_duplicate_message_reuses_response_and_side_effects(tmp_path: Path) -> None:
    environment = AssistantEnvironment(tmp_path / "duplicate.sqlite3", FULL_TRIP_COMMAND)
    try:
        session_id = environment.service.create().state.session_id
        request = AssistantMessageRequest(message_id=uuid4(), content="杭州去上海玩三天")

        first, second = await asyncio.gather(
            environment.service.send(session_id, request),
            environment.service.send(session_id, request),
        )

        assert first == second
        assert len(environment.model.calls) == 1
        assert len(environment.runtime.calls) == 1
        assert len((await environment.service.get(session_id)).turns) == 1
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_same_message_id_with_different_content_conflicts(tmp_path: Path) -> None:
    environment = AssistantEnvironment(tmp_path / "conflict.sqlite3", DISCOVERY_COMMAND)
    try:
        session_id = environment.service.create().state.session_id
        message_id = uuid4()
        await environment.service.send(
            session_id,
            AssistantMessageRequest(message_id=message_id, content="我想去广西看历史景点"),
        )

        with pytest.raises(AppError) as caught:
            await environment.service.send(
                session_id,
                AssistantMessageRequest(message_id=message_id, content="改去上海"),
            )

        assert caught.value.code == "assistant_message_conflict"
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_discovery_uses_llm_then_fast_parser(tmp_path: Path) -> None:
    environment = AssistantEnvironment(tmp_path / "discovery.sqlite3", DISCOVERY_COMMAND)
    try:
        session_id = environment.service.create().state.session_id
        first = await environment.service.send(
            session_id,
            AssistantMessageRequest(message_id=uuid4(), content="我想去广西看历史景点"),
        )
        second = await environment.service.send(
            session_id,
            AssistantMessageRequest(message_id=uuid4(), content="三天，预算两千元"),
        )

        assert first.missing_slots == ["days", "budget"]
        assert second.state.status == "recommendation_ready"
        assert second.planning_session_id is None
        assert len(environment.model.calls) == 1
        assert len(environment.runtime.calls) == 0
        assert second.state.token_usage.model_calls == 1
        assert second.state.token_usage.total_tokens > 0
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_explicit_memory_survives_new_session_and_can_be_forgotten(
    tmp_path: Path,
) -> None:
    environment = AssistantEnvironment(tmp_path / "memory.sqlite3", configured=False)
    try:
        first_id = environment.service.create().state.session_id
        remembered = await environment.service.send(
            first_id,
            AssistantMessageRequest(message_id=uuid4(), content="记住我从杭州出发"),
        )

        assert "已记住" in remembered.reply
        assert environment.repository.list_memories()[0].value == "杭州"

        second = environment.service.create()
        assert second.state.slots.origin == "杭州"
        assert second.state.slot_metadata["origin"].source == "memory"

        forgotten = await environment.service.send(
            second.state.session_id,
            AssistantMessageRequest(message_id=uuid4(), content="忘记我的出发地"),
        )

        assert "已忘记" in forgotten.reply
        assert environment.repository.list_memories() == []
        assert environment.service.create().state.slots.origin is None
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_context_ref_records_memory_version_without_body(tmp_path: Path) -> None:
    environment = AssistantEnvironment(tmp_path / "memory-ref.sqlite3", DISCOVERY_COMMAND)
    try:
        first = environment.service.create().state.session_id
        await environment.service.send(
            first,
            AssistantMessageRequest(message_id=uuid4(), content="记住我从杭州出发"),
        )
        second = environment.service.create().state.session_id
        await environment.service.send(
            second,
            AssistantMessageRequest(message_id=uuid4(), content="我想去广西看历史景点"),
        )

        metadata = environment.conversation_store.list_turns(str(second))[0].metadata
        refs = metadata["context_ref"]["memory_refs"]
        assert refs == [{"id": "travel:origin", "version": 1}]
        assert "杭州" not in str(refs)
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_model_failure_does_not_change_state(tmp_path: Path) -> None:
    environment = AssistantEnvironment(
        tmp_path / "failure.sqlite3",
        '{"commands":[{"type":"unknown"}]}',
        '{"commands":[{"type":"still_unknown"}]}',
    )
    try:
        session_id = environment.service.create().state.session_id

        with pytest.raises(AppError) as caught:
            await environment.service.send(
                session_id,
                AssistantMessageRequest(message_id=uuid4(), content="帮我规划旅行"),
            )

        restored = await environment.service.get(session_id)
        assert caught.value.code == "intent_invalid_output"
        assert restored.state.revision == 0
        assert restored.turns == []
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_unconfigured_model_uses_stable_error(tmp_path: Path) -> None:
    environment = AssistantEnvironment(tmp_path / "unconfigured.sqlite3", configured=False)
    try:
        session_id = environment.service.create().state.session_id

        with pytest.raises(AppError) as caught:
            await environment.service.send(
                session_id,
                AssistantMessageRequest(message_id=uuid4(), content="我想去广西玩"),
            )

        assert caught.value.code == "intent_not_configured"
        assert environment.repository.get_dialogue(session_id).revision == 0  # type: ignore[union-attr]
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_cancel_uses_fast_parser_without_model(tmp_path: Path) -> None:
    environment = AssistantEnvironment(tmp_path / "cancel.sqlite3", configured=False)
    try:
        session_id = environment.service.create().state.session_id

        response = await environment.service.send(
            session_id,
            AssistantMessageRequest(message_id=uuid4(), content="取消"),
        )

        assert response.state.status == "closed"
        assert response.reply == "已取消当前旅行需求。"
        assert environment.model.calls == []
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_changing_budget_starts_new_idempotent_revision(tmp_path: Path) -> None:
    change_budget = '{"commands":[{"type":"set_slot","name":"budget","value":3000}]}'
    environment = AssistantEnvironment(
        tmp_path / "change.sqlite3", FULL_TRIP_COMMAND, change_budget
    )
    try:
        session_id = environment.service.create().state.session_id
        await environment.service.send(
            session_id,
            AssistantMessageRequest(message_id=uuid4(), content="杭州去上海玩三天"),
        )

        changed = await environment.service.send(
            session_id,
            AssistantMessageRequest(message_id=uuid4(), content="预算改成三千"),
        )

        assert changed.state.slots.budget == 3000
        assert len(environment.runtime.calls) == 2
        assert environment.runtime.calls[1][1] == f"assistant:{session_id}:2"
    finally:
        environment.close()


@pytest.mark.asyncio
async def test_conversation_failure_does_not_rollback_dialogue_state(tmp_path: Path) -> None:
    database = tmp_path / "conversation-failure.sqlite3"
    repository = SqliteTripRepository(str(database))
    runtime = FakePlanningRuntime()
    model = SequenceModel(DISCOVERY_COMMAND)
    service = TravelAssistantService(
        repository,
        FailingConversationManager(),
        FakeCityResolver(),
        runtime,
        TravelCommandGenerator(model),
    )
    session_id = service.create().state.session_id

    response = await service.send(
        session_id,
        AssistantMessageRequest(message_id=uuid4(), content="我想去广西看历史景点"),
    )

    assert response.state.revision == 1
    assert repository.get_dialogue(session_id).revision == 1  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_state_and_turns_survive_service_restart(tmp_path: Path) -> None:
    database = tmp_path / "restart.sqlite3"
    first = AssistantEnvironment(database, DISCOVERY_COMMAND)
    session_id = first.service.create().state.session_id
    await first.service.send(
        session_id,
        AssistantMessageRequest(message_id=uuid4(), content="我想去广西看历史景点"),
    )
    first.close()

    second = AssistantEnvironment(database, configured=False)
    try:
        restored = await second.service.get(session_id)

        assert restored.state.slots.destination_region == "广西壮族自治区"
        assert restored.turns[0].assistant_content
    finally:
        second.close()


@pytest.mark.asyncio
async def test_assistant_api_create_send_and_restore(tmp_path: Path) -> None:
    environment = AssistantEnvironment(tmp_path / "api.sqlite3", DISCOVERY_COMMAND)
    app.dependency_overrides[get_assistant_service] = lambda: environment.service
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            created = await client.post("/api/assistant-sessions")
            session_id = created.json()["state"]["session_id"]
            sent = await client.post(
                f"/api/assistant-sessions/{session_id}/messages",
                json={
                    "message_id": str(uuid4()),
                    "content": "我想去广西看历史景点",
                },
            )
            restored = await client.get(f"/api/assistant-sessions/{session_id}")

        assert created.status_code == 201
        assert sent.status_code == 200
        assert sent.json()["missing_slots"] == ["days", "budget"]
        assert restored.status_code == 200
        assert len(restored.json()["turns"]) == 1
    finally:
        app.dependency_overrides.pop(get_assistant_service, None)
        environment.close()


@pytest.mark.asyncio
async def test_assistant_api_returns_stable_not_found_error(tmp_path: Path) -> None:
    environment = AssistantEnvironment(tmp_path / "not-found.sqlite3", configured=False)
    app.dependency_overrides[get_assistant_service] = lambda: environment.service
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(f"/api/assistant-sessions/{uuid4()}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "assistant_session_not_found"
    finally:
        app.dependency_overrides.pop(get_assistant_service, None)
        environment.close()


@pytest.mark.asyncio
async def test_assistant_memory_api_lists_and_deletes_explicit_memory(
    tmp_path: Path,
) -> None:
    environment = AssistantEnvironment(tmp_path / "memory-api.sqlite3", configured=False)
    session_id = environment.service.create().state.session_id
    await environment.service.send(
        session_id,
        AssistantMessageRequest(message_id=uuid4(), content="记住我从杭州出发"),
    )
    app.dependency_overrides[get_assistant_service] = lambda: environment.service
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            listed = await client.get("/api/assistant-memories")
            deleted = await client.delete("/api/assistant-memories/origin")
            empty = await client.get("/api/assistant-memories")

        assert listed.status_code == 200
        assert listed.json()[0]["value"] == "杭州"
        assert deleted.status_code == 204
        assert empty.json() == []
    finally:
        app.dependency_overrides.pop(get_assistant_service, None)
        environment.close()
