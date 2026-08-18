"""PostgreSQL 共享状态、匿名隔离和一次性迁移集成测试。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from re_zlagent.harness.conversation import (  # type: ignore[import-untyped]
    ConversationManager,
    PooledPostgresConversationStore,
)

from app.config import Settings
from app.errors import VisitorClaimError
from app.identity import AnonymousIdentityService
from app.main import app, get_identity_service, get_travel_service, get_visitor_id
from app.models import PlanningSession
from app.storage import PostgresTravelRepository, create_conversation_pool
from app.travel import TravelService
from scripts.migrate_sqlite_to_postgres import LEGACY_NAMESPACE, migrate_sqlite
from tests.fakes import FakeMapProvider, FakePlanner, sample_request
from tests.sqlite_repository import SqliteTripRepository
from tests.test_workbench import planning_request

_TEST_VISITORS: dict[int, list[UUID]] = {}


@pytest.fixture
def postgres_repository() -> Iterator[PostgresTravelRepository]:
    """连接本地测试数据库，并在测试后删除本轮访客资源。"""

    settings = Settings()
    if not settings.database_url:
        pytest.skip("未配置 DATABASE_URL")
    repository = PostgresTravelRepository(
        settings.database_url,
        min_size=1,
        max_size=4,
        timeout_seconds=3,
    )
    visitor_ids: list[UUID] = []
    _TEST_VISITORS[id(repository)] = visitor_ids
    try:
        if repository.readiness() != "ready":
            pytest.skip("PostgreSQL app Schema 不可用")
        yield repository
    finally:
        _cleanup_visitors(repository, visitor_ids)
        _TEST_VISITORS.pop(id(repository), None)
        repository.close()


def test_two_cookie_clients_cannot_read_each_others_trip(
    postgres_repository: PostgresTravelRepository,
) -> None:
    """浏览器 Cookie 不同，即使知道行程 UUID 也只能得到 404。"""

    repository = postgres_repository
    service = TravelService(FakeMapProvider(), FakePlanner(), repository)
    identity = AnonymousIdentityService(repository)
    app.dependency_overrides.pop(get_visitor_id, None)
    app.dependency_overrides[get_travel_service] = lambda: service
    app.dependency_overrides[get_identity_service] = lambda: identity
    first = TestClient(app)
    second = TestClient(app)
    try:
        created = first.post("/api/trips", json=sample_request().model_dump(mode="json"))
        assert created.status_code == 201
        trip_id = created.json()["trip_id"]
        _remember_cookie_visitors(repository, first, second)

        assert first.get(f"/api/trips/{trip_id}").status_code == 200
        hidden = second.get(f"/api/trips/{trip_id}")

        assert hidden.status_code == 404
    finally:
        app.dependency_overrides.pop(get_travel_service, None)
        app.dependency_overrides.pop(get_identity_service, None)


def test_idempotency_is_scoped_by_visitor(
    postgres_repository: PostgresTravelRepository,
) -> None:
    """同一访客复用幂等结果，不同访客可以使用相同业务键。"""

    repository = postgres_repository
    first = _new_visitor(repository)
    second = _new_visitor(repository)
    now = _now()
    first_session = PlanningSession(
        session_id=uuid4(), request=planning_request(), created_at=now, updated_at=now
    )
    second_session = first_session.model_copy(update={"session_id": uuid4()})

    created = repository.create_session(first_session, "same-key", first)
    duplicate = repository.create_session(second_session, "same-key", first)
    other_visitor = repository.create_session(second_session, "same-key", second)

    assert duplicate.session_id == created.session_id
    assert other_visitor.session_id == second_session.session_id
    assert repository.get_session(created.session_id, second) is None


def test_trip_revision_uses_conditional_update(
    postgres_repository: PostgresTravelRepository,
) -> None:
    """两个基于同一旧版本的编辑只能提交一个。"""

    repository = postgres_repository
    visitor_id = _new_visitor(repository)
    service = TravelService(FakeMapProvider(), FakePlanner(), repository)
    itinerary = service.create(sample_request(), visitor_id)
    updated = itinerary.model_copy(update={"revision": itinerary.revision + 1})

    first = repository.save_if_revision(updated, sample_request(), 1, visitor_id)
    second = repository.save_if_revision(updated, sample_request(), 1, visitor_id)

    assert first is True
    assert second is False


def test_claim_code_can_only_succeed_once(
    postgres_repository: PostgresTravelRepository,
) -> None:
    """认领在同一事务中转移资源，并立即使一次性凭据失效。"""

    repository = postgres_repository
    legacy_id = _new_visitor(repository)
    current_id = _new_visitor(repository)
    service = TravelService(FakeMapProvider(), FakePlanner(), repository)
    itinerary = service.create(sample_request(), legacy_id)
    token = "claim-" + uuid4().hex
    _insert_claim(repository, legacy_id, token)

    identity = AnonymousIdentityService(repository)
    identity.claim(current_id, token)

    assert repository.get(itinerary.trip_id, legacy_id) is None
    assert repository.get(itinerary.trip_id, current_id) is not None
    with pytest.raises(VisitorClaimError) as captured:
        identity.claim(current_id, token)
    assert captured.value.code == "visitor_claim_used"


def test_first_claim_request_keeps_anonymous_cookie(
    postgres_repository: PostgresTravelRepository,
) -> None:
    """首次请求认领成功后必须保留匿名 Cookie，避免下一请求变成新访客。"""

    repository = postgres_repository
    legacy_id = _new_visitor(repository)
    token = "claim-" + uuid4().hex
    _insert_claim(repository, legacy_id, token)
    identity = AnonymousIdentityService(repository)
    app.dependency_overrides.pop(get_visitor_id, None)
    app.dependency_overrides[get_identity_service] = lambda: identity
    client = TestClient(app)
    try:
        response = client.post("/api/visitor/claim", json={"token": token})
        assert response.status_code == 204
        assert client.cookies.get("openzltravelvisitor")
        _remember_cookie_visitors(repository, client)
    finally:
        app.dependency_overrides.pop(get_identity_service, None)


def test_sqlite_migration_is_atomic_and_rejects_duplicate_source(
    tmp_path: Path,
    postgres_repository: PostgresTravelRepository,
) -> None:
    """迁移保留 UUID 和 JSON，并用源文件哈希拒绝重复导入。"""

    source = tmp_path / "legacy.sqlite3"
    sqlite_repository = SqliteTripRepository(str(source))
    legacy_service = TravelService(FakeMapProvider(), FakePlanner(), sqlite_repository)
    itinerary = legacy_service.create(sample_request())
    now = _now()
    session = PlanningSession(
        session_id=uuid4(), request=planning_request(), created_at=now, updated_at=now
    )
    sqlite_repository.create_session(session, "migration-test")
    sqlite_repository.set_cache("migration-test", uuid4().hex, {"ok": True}, 60)

    claim_token, counts = migrate_sqlite(source, Settings().database_url)
    source_hash = _sha256(source)
    legacy_id = _legacy_visitor_id(postgres_repository, source_hash)
    _visitor_list(postgres_repository).append(legacy_id)

    assert counts["trips"] == 1
    assert counts["planning_sessions"] == 1
    assert counts["provider_cache_skipped"] == 1
    assert postgres_repository.get(itinerary.trip_id, legacy_id) is not None
    assert len(claim_token) >= 32
    with pytest.raises(RuntimeError, match="已经迁移"):
        migrate_sqlite(source, Settings().database_url)
    _cleanup_import(postgres_repository, source_hash)


@pytest.mark.asyncio
async def test_openzlagent_postgres_turn_sequence_is_continuous() -> None:
    """OpenZLAgent 固定表在独立连接池中仍保持连续轮次。"""

    settings = Settings()
    if not settings.database_url:
        pytest.skip("未配置 DATABASE_URL")
    pool = create_conversation_pool(settings.database_url, min_size=1, max_size=2)
    store = PooledPostgresConversationStore(pool)
    manager = ConversationManager(store)
    session_id = str(uuid4())
    try:
        await manager.record_turn(session_id, "第一问", "第一答")
        await manager.record_turn(session_id, "第二问", "第二答")
        context = await manager.prepare(session_id)

        assert [turn.sequence for turn in context.recent_turns] == [1, 2]
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM app.session_summaries WHERE session_id = %s",
                (session_id,),
            )
            connection.execute("DELETE FROM app.session_turns WHERE session_id = %s", (session_id,))
        pool.close()


def _new_visitor(repository: PostgresTravelRepository) -> UUID:
    token_hash = hashlib.sha256(uuid4().bytes).hexdigest()
    visitor_id = repository.get_or_create_visitor(
        token_hash,
        _now() + timedelta(days=1),
    )
    _visitor_list(repository).append(visitor_id)
    return visitor_id


def _remember_cookie_visitors(
    repository: PostgresTravelRepository,
    *clients: TestClient,
) -> None:
    for client in clients:
        response = client.get("/api/trips")
        assert response.status_code == 200
        token = client.cookies.get("openzltravelvisitor")
        assert token
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = _fetch_one(
            repository,
            "SELECT visitorid FROM app.visitor WHERE tokenhash = %s",
            token_hash,
        )
        _visitor_list(repository).append(UUID(str(row["visitorid"])))


def _insert_claim(
    repository: PostgresTravelRepository,
    legacy_id: UUID,
    token: str,
) -> None:
    with repository.pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO app.legacyclaim
                (claimid, visitorid, tokenhash, status, createdat, expiresat)
            VALUES (%s, %s, %s, 'pending', %s, %s)
            """,
            (
                uuid4(),
                legacy_id,
                hashlib.sha256(token.encode()).hexdigest(),
                _now(),
                _now() + timedelta(hours=1),
            ),
        )


def _legacy_visitor_id(repository: PostgresTravelRepository, source_hash: str) -> UUID:
    del repository
    return uuid5(LEGACY_NAMESPACE, source_hash)


def _cleanup_visitors(
    repository: PostgresTravelRepository,
    visitor_ids: list[UUID],
) -> None:
    unique_ids = list(dict.fromkeys(visitor_ids))
    if not unique_ids:
        return
    with repository.pool.connection() as connection:
        session_rows = connection.execute(
            "SELECT sessionid FROM app.dialoguesession WHERE visitorid = ANY(%s)",
            (unique_ids,),
        ).fetchall()
        session_ids = [str(row["sessionid"]) for row in session_rows]
        if session_ids:
            connection.execute(
                "DELETE FROM app.session_summaries WHERE session_id = ANY(%s)",
                (session_ids,),
            )
            connection.execute(
                "DELETE FROM app.session_turns WHERE session_id = ANY(%s)",
                (session_ids,),
            )
        tables = (
            "dialoguerequest",
            "travelmemory",
            "trip",
            "planningsession",
            "dialoguesession",
        )
        for table in tables:
            connection.execute(f"DELETE FROM app.{table} WHERE visitorid = ANY(%s)", (unique_ids,))
        connection.execute(
            "DELETE FROM app.legacyclaim WHERE visitorid = ANY(%s) OR claimedby = ANY(%s)",
            (unique_ids, unique_ids),
        )
        connection.execute("DELETE FROM app.visitor WHERE visitorid = ANY(%s)", (unique_ids,))


def _cleanup_import(repository: PostgresTravelRepository, source_hash: str) -> None:
    with repository.pool.connection() as connection:
        connection.execute("DELETE FROM app.importrecord WHERE sourcehash = %s", (source_hash,))


def _fetch_one(
    repository: PostgresTravelRepository,
    query: str,
    parameter: object | None = None,
) -> dict[str, object]:
    params = () if parameter is None else (parameter,)
    with repository.pool.connection() as connection:
        row = connection.execute(query, params).fetchone()
    assert row is not None
    return row


def _visitor_list(repository: PostgresTravelRepository) -> list[UUID]:
    return _TEST_VISITORS[id(repository)]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)
