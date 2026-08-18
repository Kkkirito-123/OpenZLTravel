"""PostgreSQL 业务持久化。

本模块集中访客、行程、规划、对话和长期偏好。业务服务只传入
``visitorid`` 和领域模型，不接触 psycopg 行、SQL 或连接池对象。
"""

from __future__ import annotations

import builtins
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg import Error as PsycopgError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

from app.errors import DatabaseUnavailableError
from app.models import (
    AssistantTurnResponse,
    Itinerary,
    MemorySlotName,
    PlanningSession,
    TravelDialogueState,
    TravelMemory,
    TravelRequest,
    TripSummary,
)

# 零 UUID 只用于旧内部调用和测试兼容；真实 HTTP 请求始终由匿名 Cookie 注入随机访客 ID。
UNSCOPED_VISITOR_ID = UUID(int=0)


class RepositoryConflictError(Exception):
    """数据库乐观锁或幂等约束冲突。"""


class TripRepository(Protocol):
    """行程持久化接口。"""

    def save(self, itinerary: Itinerary, request: TravelRequest, visitor_id: UUID) -> None:
        """保存已完成行程。"""

    def save_if_revision(
        self,
        itinerary: Itinerary,
        request: TravelRequest,
        expected_revision: int,
        visitor_id: UUID,
    ) -> bool:
        """按期望版本提交编辑。"""

    def get(self, trip_id: UUID, visitor_id: UUID) -> Itinerary | None:
        """读取当前访客的行程。"""

    def list(self, visitor_id: UUID) -> list[TripSummary]:
        """列出当前访客的行程。"""

    def delete(self, trip_id: UUID, visitor_id: UUID) -> bool:
        """删除当前访客的行程。"""

    def get_request(self, trip_id: UUID, visitor_id: UUID) -> TravelRequest | None:
        """读取行程请求快照。"""


class PlanningRepository(Protocol):
    """规划运行时持久化接口。"""

    def create_session(
        self,
        session: PlanningSession,
        idempotency_key: str | None,
        visitor_id: UUID,
    ) -> PlanningSession:
        """按访客和幂等键创建会话。"""

    def save_session(self, session: PlanningSession, visitor_id: UUID) -> None:
        """保存规划会话。"""

    def get_session(self, session_id: UUID, visitor_id: UUID) -> PlanningSession | None:
        """读取当前访客的规划会话。"""

    def delete_session(self, session_id: UUID, visitor_id: UUID) -> bool:
        """删除当前访客的规划会话。"""

    def list_recoverable_sessions(self) -> list[tuple[UUID, PlanningSession]]:
        """返回全部访客中断的任务及其所有者。"""


class DialogueRepository(Protocol):
    """旅行助手状态、幂等消息和长期偏好接口。"""

    def create_dialogue(self, state: TravelDialogueState, visitor_id: UUID) -> None:
        """创建当前访客的助手会话。"""

    def get_dialogue(self, session_id: UUID, visitor_id: UUID) -> TravelDialogueState | None:
        """读取当前访客的助手会话。"""

    def get_dialogue_response(
        self, session_id: UUID, message_id: UUID, visitor_id: UUID
    ) -> tuple[str, AssistantTurnResponse] | None:
        """读取一条幂等消息结果。"""

    def list_memories(self, visitor_id: UUID) -> builtins.list[TravelMemory]:
        """读取当前访客长期偏好。"""

    def delete_memory(self, key: MemorySlotName, visitor_id: UUID) -> bool:
        """删除当前访客一项长期偏好。"""

    def save_dialogue_response(
        self,
        state: TravelDialogueState,
        message_id: UUID,
        request_content: str,
        response: AssistantTurnResponse,
        visitor_id: UUID,
        memory_upserts: dict[MemorySlotName, str | builtins.list[str]] | None = None,
        memory_deletes: set[MemorySlotName] | None = None,
    ) -> None:
        """原子保存状态、响应与偏好变更。"""


class PostgresTravelRepository:
    """使用有界连接池保存多人共享业务状态。"""

    def __init__(
        self,
        database_url: str,
        min_size: int = 2,
        max_size: int = 20,
        timeout_seconds: float = 5,
        pool: Any | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._pool = pool if pool is not None else _create_pool(
            database_url, min_size, max_size, timeout_seconds
        )

    @property
    def pool(self) -> Any:
        """仅供同模块基础设施复用；业务服务不得访问。"""

        return self._pool

    def close(self) -> None:
        """关闭业务连接池。"""

        if self._pool is not None:
            self._pool.close()

    def readiness(self) -> str:
        """检查 app Schema 是否可读。"""

        try:
            with self._connection() as connection:
                connection.execute("SELECT version FROM app.schemaversion LIMIT 1").fetchone()
        except DatabaseUnavailableError:
            return "unavailable"
        return "ready"

    def get_or_create_visitor(self, token_hash: str, expires_at: datetime) -> UUID:
        """只保存随机 Token 哈希，泄露数据库时不能还原浏览器 Cookie。"""

        visitor_id = uuid4()
        now = _now()
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO app.visitor
                    (visitorid, tokenhash, createdat, lastseenat, expiresat)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tokenhash) DO UPDATE SET
                    lastseenat = excluded.lastseenat,
                    expiresat = GREATEST(app.visitor.expiresat, excluded.expiresat)
                RETURNING visitorid
                """,
                (visitor_id, token_hash, now, now, expires_at),
            ).fetchone()
        if row is None:
            raise DatabaseUnavailableError("创建匿名访客失败")
        return UUID(str(row["visitorid"]))

    def save(self, itinerary: Itinerary, request: TravelRequest, visitor_id: UUID) -> None:
        """全部事实校验完成后保存；失败流程不得留下半成品。"""

        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO app.trip
                    (tripid, visitorid, destination, startdate, enddate, summary,
                     createdat, requestjson, itineraryjson, revision)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tripid) DO UPDATE SET
                    destination = excluded.destination,
                    startdate = excluded.startdate,
                    enddate = excluded.enddate,
                    summary = excluded.summary,
                    requestjson = excluded.requestjson,
                    itineraryjson = excluded.itineraryjson,
                    revision = excluded.revision
                WHERE app.trip.visitorid = excluded.visitorid
                """,
                _trip_values(itinerary, request, visitor_id),
            )

    def save_if_revision(
        self,
        itinerary: Itinerary,
        request: TravelRequest,
        expected_revision: int,
        visitor_id: UUID,
    ) -> bool:
        """用条件 UPDATE 保证并发编辑只有一个版本成功。"""

        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE app.trip SET
                    destination = %s, startdate = %s, enddate = %s, summary = %s,
                    requestjson = %s, itineraryjson = %s, revision = %s
                WHERE tripid = %s AND visitorid = %s AND revision = %s
                """,
                (
                    itinerary.destination,
                    itinerary.start_date,
                    itinerary.end_date,
                    itinerary.summary,
                    Jsonb(request.model_dump(mode="json")),
                    Jsonb(itinerary.model_dump(mode="json")),
                    itinerary.revision,
                    itinerary.trip_id,
                    visitor_id,
                    expected_revision,
                ),
            )
        return cursor.rowcount == 1

    def get(self, trip_id: UUID, visitor_id: UUID) -> Itinerary | None:
        """资源 ID 与 visitorid 必须同时匹配，避免跨访客枚举。"""

        row = self._fetch_one(
            "SELECT itineraryjson FROM app.trip WHERE tripid = %s AND visitorid = %s",
            (trip_id, visitor_id),
        )
        return Itinerary.model_validate(row["itineraryjson"]) if row else None

    def get_request(self, trip_id: UUID, visitor_id: UUID) -> TravelRequest | None:
        """读取当前访客的原始需求快照。"""

        row = self._fetch_one(
            "SELECT requestjson FROM app.trip WHERE tripid = %s AND visitorid = %s",
            (trip_id, visitor_id),
        )
        return TravelRequest.model_validate(row["requestjson"]) if row else None

    def list(self, visitor_id: UUID) -> builtins.list[TripSummary]:
        """按创建时间倒序列出当前访客行程。"""

        rows = self._fetch_all(
            """
            SELECT tripid, destination, startdate, enddate, summary, createdat
            FROM app.trip WHERE visitorid = %s ORDER BY createdat DESC
            """,
            (visitor_id,),
        )
        return [_trip_summary(row) for row in rows]

    def delete(self, trip_id: UUID, visitor_id: UUID) -> bool:
        """删除当前访客的行程。"""

        return self._delete(
            "DELETE FROM app.trip WHERE tripid = %s AND visitorid = %s",
            (trip_id, visitor_id),
        )

    def create_session(
        self,
        session: PlanningSession,
        idempotency_key: str | None,
        visitor_id: UUID,
    ) -> PlanningSession:
        """数据库唯一约束是幂等的最终保障，不能只依赖进程内缓存。"""

        values = (
            session.session_id,
            visitor_id,
            idempotency_key,
            session.status,
            Jsonb(session.request.model_dump(mode="json")),
            Jsonb(session.model_dump(mode="json")),
            session.created_at,
            session.updated_at,
        )
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO app.planningsession
                    (sessionid, visitorid, idempotencykey, status, requestjson,
                     sessionjson, createdat, updatedat)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (visitorid, idempotencykey) DO NOTHING
                RETURNING sessionjson
                """,
                values,
            ).fetchone()
            if row is None and idempotency_key:
                row = connection.execute(
                    """
                    SELECT sessionjson FROM app.planningsession
                    WHERE visitorid = %s AND idempotencykey = %s
                    """,
                    (visitor_id, idempotency_key),
                ).fetchone()
        return PlanningSession.model_validate(row["sessionjson"]) if row else session

    def save_session(self, session: PlanningSession, visitor_id: UUID) -> None:
        """保存当前访客的规划会话快照。"""

        with self._connection() as connection:
            connection.execute(
                """
                UPDATE app.planningsession
                SET status = %s, sessionjson = %s, updatedat = %s
                WHERE sessionid = %s AND visitorid = %s
                """,
                (
                    session.status,
                    Jsonb(session.model_dump(mode="json")),
                    session.updated_at,
                    session.session_id,
                    visitor_id,
                ),
            )

    def get_session(self, session_id: UUID, visitor_id: UUID) -> PlanningSession | None:
        """读取当前访客的规划会话。"""

        row = self._fetch_one(
            """
            SELECT sessionjson FROM app.planningsession
            WHERE sessionid = %s AND visitorid = %s
            """,
            (session_id, visitor_id),
        )
        return PlanningSession.model_validate(row["sessionjson"]) if row else None

    def delete_session(self, session_id: UUID, visitor_id: UUID) -> bool:
        """删除当前访客的规划会话。"""

        return self._delete(
            "DELETE FROM app.planningsession WHERE sessionid = %s AND visitorid = %s",
            (session_id, visitor_id),
        )

    def list_recoverable_sessions(
        self,
    ) -> builtins.list[tuple[UUID, PlanningSession]]:
        """启动恢复必须携带所有者，恢复任务才能继续保持隔离。"""

        rows = self._fetch_all(
            """
            SELECT visitorid, sessionjson FROM app.planningsession
            WHERE status IN ('searching', 'generating') ORDER BY createdat
            """,
            (),
        )
        return [
            (UUID(str(row["visitorid"])), PlanningSession.model_validate(row["sessionjson"]))
            for row in rows
        ]

    def create_dialogue(self, state: TravelDialogueState, visitor_id: UUID) -> None:
        """创建访客专属助手状态。"""

        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO app.dialoguesession
                    (sessionid, visitorid, revision, status, activeflow, statejson,
                     planningsessionid, createdat, updatedat)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    state.session_id,
                    visitor_id,
                    state.revision,
                    state.status,
                    state.active_flow,
                    Jsonb(state.model_dump(mode="json")),
                    state.planning_session_id,
                    state.created_at,
                    state.updated_at,
                ),
            )

    def get_dialogue(self, session_id: UUID, visitor_id: UUID) -> TravelDialogueState | None:
        """读取访客专属助手状态。"""

        row = self._fetch_one(
            """
            SELECT statejson FROM app.dialoguesession
            WHERE sessionid = %s AND visitorid = %s
            """,
            (session_id, visitor_id),
        )
        return TravelDialogueState.model_validate(row["statejson"]) if row else None

    def get_dialogue_response(
        self, session_id: UUID, message_id: UUID, visitor_id: UUID
    ) -> tuple[str, AssistantTurnResponse] | None:
        """读取当前访客的幂等消息结果。"""

        row = self._fetch_one(
            """
            SELECT requestcontent, responsejson FROM app.dialoguerequest
            WHERE sessionid = %s AND messageid = %s AND visitorid = %s
            """,
            (session_id, message_id, visitor_id),
        )
        if row is None:
            return None
        return str(row["requestcontent"]), AssistantTurnResponse.model_validate(
            row["responsejson"]
        )

    def list_memories(self, visitor_id: UUID) -> builtins.list[TravelMemory]:
        """列出当前访客明确保存的长期偏好。"""

        rows = self._fetch_all(
            "SELECT * FROM app.travelmemory WHERE visitorid = %s ORDER BY memorykey",
            (visitor_id,),
        )
        return [_travel_memory(row) for row in rows]

    def delete_memory(self, key: MemorySlotName, visitor_id: UUID) -> bool:
        """删除当前访客的一项长期偏好。"""

        return self._delete(
            "DELETE FROM app.travelmemory WHERE visitorid = %s AND memorykey = %s",
            (visitor_id, key),
        )

    def save_dialogue_response(
        self,
        state: TravelDialogueState,
        message_id: UUID,
        request_content: str,
        response: AssistantTurnResponse,
        visitor_id: UUID,
        memory_upserts: dict[MemorySlotName, str | builtins.list[str]] | None = None,
        memory_deletes: set[MemorySlotName] | None = None,
    ) -> None:
        """在一个事务中提交状态、幂等响应和长期偏好。"""

        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE app.dialoguesession SET
                    revision = %s, status = %s, activeflow = %s, statejson = %s,
                    planningsessionid = %s, updatedat = %s
                WHERE sessionid = %s AND visitorid = %s AND revision = %s
                """,
                (
                    state.revision,
                    state.status,
                    state.active_flow,
                    Jsonb(state.model_dump(mode="json")),
                    state.planning_session_id,
                    state.updated_at,
                    state.session_id,
                    visitor_id,
                    state.revision - 1,
                ),
            )
            if cursor.rowcount != 1:
                raise RepositoryConflictError("dialogue revision conflict")
            inserted = connection.execute(
                """
                INSERT INTO app.dialoguerequest
                    (sessionid, visitorid, messageid, requestcontent, responsejson, createdat)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (sessionid, messageid) DO NOTHING RETURNING messageid
                """,
                (
                    state.session_id,
                    visitor_id,
                    message_id,
                    request_content,
                    Jsonb(response.model_dump(mode="json")),
                    state.updated_at,
                ),
            ).fetchone()
            if inserted is None:
                raise RepositoryConflictError("dialogue message conflict")
            self._apply_memory_changes(
                connection,
                state,
                visitor_id,
                memory_upserts or {},
                memory_deletes or set(),
            )

    def claim_legacy(self, visitor_id: UUID, token_hash: str) -> None:
        """原子认领旧数据；当前访客偏好优先，旧偏好只补缺失项。"""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM app.legacyclaim WHERE tokenhash = %s FOR UPDATE
                """,
                (token_hash,),
            ).fetchone()
            _validate_claim(row)
            assert row is not None
            legacy_id = UUID(str(row["visitorid"]))
            self._move_legacy_resources(connection, legacy_id, visitor_id)
            connection.execute(
                """
                UPDATE app.legacyclaim SET status = 'claimed', claimedby = %s, claimedat = %s
                WHERE claimid = %s AND status = 'pending'
                """,
                (visitor_id, _now(), row["claimid"]),
            )

    @staticmethod
    def _apply_memory_changes(
        connection: Connection[Any],
        state: TravelDialogueState,
        visitor_id: UUID,
        upserts: dict[MemorySlotName, str | builtins.list[str]],
        deletes: set[MemorySlotName],
    ) -> None:
        for key in deletes:
            connection.execute(
                "DELETE FROM app.travelmemory WHERE visitorid = %s AND memorykey = %s",
                (visitor_id, key),
            )
        for key, value in upserts.items():
            connection.execute(
                """
                INSERT INTO app.travelmemory
                    (visitorid, memorykey, valuejson, version, sourcesessionid,
                     createdat, updatedat)
                VALUES (%s, %s, %s, 1, %s, %s, %s)
                ON CONFLICT (visitorid, memorykey) DO UPDATE SET
                    valuejson = excluded.valuejson,
                    version = app.travelmemory.version + 1,
                    sourcesessionid = excluded.sourcesessionid,
                    updatedat = excluded.updatedat
                """,
                (
                    visitor_id,
                    key,
                    Jsonb(value),
                    state.session_id,
                    state.updated_at,
                    state.updated_at,
                ),
            )

    @staticmethod
    def _move_legacy_resources(
        connection: Connection[Any], legacy_id: UUID, visitor_id: UUID
    ) -> None:
        connection.execute(
            """
            UPDATE app.planningsession AS old SET idempotencykey = NULL
            WHERE old.visitorid = %s AND old.idempotencykey IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM app.planningsession AS current
                  WHERE current.visitorid = %s
                    AND current.idempotencykey = old.idempotencykey
              )
            """,
            (legacy_id, visitor_id),
        )
        for table in ("trip", "planningsession", "dialoguesession", "dialoguerequest"):
            connection.execute(
                f"UPDATE app.{table} SET visitorid = %s WHERE visitorid = %s",
                (visitor_id, legacy_id),
            )
        connection.execute(
            """
            INSERT INTO app.travelmemory
                (visitorid, memorykey, valuejson, version, sourcesessionid, createdat, updatedat)
            SELECT %s, memorykey, valuejson, version, sourcesessionid, createdat, updatedat
            FROM app.travelmemory WHERE visitorid = %s
            ON CONFLICT (visitorid, memorykey) DO NOTHING
            """,
            (visitor_id, legacy_id),
        )
        connection.execute("DELETE FROM app.travelmemory WHERE visitorid = %s", (legacy_id,))

    @contextmanager
    def _connection(self) -> Iterator[Connection[Any]]:
        if self._pool is None:
            raise DatabaseUnavailableError("PostgreSQL 业务数据库尚未配置")
        try:
            with self._pool.connection(timeout=self.timeout_seconds) as connection:
                yield connection
        except RepositoryConflictError:
            raise
        except (PsycopgError, PoolTimeout, OSError) as error:
            raise DatabaseUnavailableError() from error

    def _fetch_one(self, query: str, parameters: tuple[Any, ...]) -> Any | None:
        with self._connection() as connection:
            return connection.execute(query, parameters).fetchone()

    def _fetch_all(
        self, query: str, parameters: tuple[Any, ...]
    ) -> builtins.list[Any]:
        with self._connection() as connection:
            return builtins.list(connection.execute(query, parameters).fetchall())

    def _delete(self, query: str, parameters: tuple[Any, ...]) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(query, parameters)
        return cursor.rowcount == 1


def _create_pool(
    database_url: str, min_size: int, max_size: int, timeout_seconds: float
) -> ConnectionPool[Any] | None:
    if not database_url:
        return None
    pool_min = max(1, min_size)
    pool: ConnectionPool[Any] = ConnectionPool(
        database_url,
        min_size=pool_min,
        max_size=max(pool_min, max_size),
        timeout=timeout_seconds,
        kwargs={"row_factory": dict_row},
        configure=_configure_connection,
        open=False,
    )
    pool.open(wait=False)
    return pool


def _configure_connection(connection: Connection[Any]) -> None:
    connection.execute("SET search_path TO app, public")
    connection.commit()


def create_conversation_pool(
    database_url: str, min_size: int = 1, max_size: int = 4, timeout_seconds: float = 5
) -> ConnectionPool[Any]:
    """为 OpenZLAgent 会话建立独立有界同步连接池。"""

    pool = _create_pool(database_url, min_size, max_size, timeout_seconds)
    if pool is None:
        raise DatabaseUnavailableError("PostgreSQL 对话数据库尚未配置")
    return pool


def _trip_values(
    itinerary: Itinerary, request: TravelRequest, visitor_id: UUID
) -> tuple[Any, ...]:
    return (
        itinerary.trip_id,
        visitor_id,
        itinerary.destination,
        itinerary.start_date,
        itinerary.end_date,
        itinerary.summary,
        itinerary.created_at,
        Jsonb(request.model_dump(mode="json")),
        Jsonb(itinerary.model_dump(mode="json")),
        itinerary.revision,
    )


def _trip_summary(row: Any) -> TripSummary:
    return TripSummary(
        trip_id=UUID(str(row["tripid"])),
        destination=str(row["destination"]),
        start_date=row["startdate"],
        end_date=row["enddate"],
        summary=str(row["summary"]),
        created_at=row["createdat"],
    )


def _travel_memory(row: Any) -> TravelMemory:
    return TravelMemory(
        key=row["memorykey"],
        value=row["valuejson"],
        version=int(row["version"]),
        source_session_id=UUID(str(row["sourcesessionid"])),
        created_at=row["createdat"],
        updated_at=row["updatedat"],
    )


def _validate_claim(row: Any | None) -> None:
    if row is None:
        raise RepositoryConflictError("visitor_claim_invalid")
    if row["status"] == "claimed":
        raise RepositoryConflictError("visitor_claim_used")
    if row["expiresat"] <= _now():
        raise RepositoryConflictError("visitor_claim_expired")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def read_sql_file(path: str | Path) -> str:
    """读取 UTF-8 迁移 SQL，供一次性工具复用。"""

    return Path(path).read_text(encoding="utf-8")
