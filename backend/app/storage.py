"""OpenZLTravel 的行程持久化接口与 SQLite 实现。

数据库只保存请求快照和最终行程 JSON。MVP 不提前拆成几十张业务表，读取时由
Pydantic 负责结构校验，既保持简单，也便于未来迁移。
"""

from __future__ import annotations

import builtins
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from app.models import (
    AssistantTurnResponse,
    CandidateCatalog,
    City,
    Itinerary,
    MemorySlotName,
    PlanningSession,
    Poi,
    TravelDialogueState,
    TravelMemory,
    TravelRequest,
    TripSummary,
)


class TripRepository(Protocol):
    """行程持久化接口，业务服务只依赖这些最小能力。"""

    def save(self, itinerary: Itinerary, request: TravelRequest) -> None:
        """保存请求与完整行程快照。"""

        ...

    def save_if_revision(
        self,
        itinerary: Itinerary,
        request: TravelRequest,
        expected_revision: int,
    ) -> bool:
        """仅当当前版本仍匹配时保存行程，返回是否成功提交。"""

        ...

    def get(self, trip_id: UUID) -> Itinerary | None:
        """按 ID 读取完整行程。"""

        ...

    def list(self) -> list[TripSummary]:
        """按创建时间倒序返回历史摘要。"""

        ...

    def delete(self, trip_id: UUID) -> bool:
        """删除行程，并返回是否命中记录。"""

        ...

    def get_request(self, trip_id: UUID) -> TravelRequest | None:
        """读取生成行程时的请求快照。"""

        ...


class PlanningRepository(Protocol):
    """规划运行时所需的会话与缓存持久化能力。"""

    def create_session(
        self, session: PlanningSession, idempotency_key: str | None = None
    ) -> PlanningSession:
        """创建会话；相同幂等键返回已经存在的会话。"""

        ...

    def save_session(self, session: PlanningSession) -> None:
        """保存完整会话快照。"""

        ...

    def get_session(self, session_id: UUID) -> PlanningSession | None:
        """按 ID 读取规划会话。"""

        ...

    def delete_session(self, session_id: UUID) -> bool:
        """删除规划会话。"""

        ...

    def list_recoverable_sessions(self) -> builtins.list[PlanningSession]:
        """返回进程重启后需要恢复的会话。"""

        ...

    def get_cache(self, provider: str, key: str) -> Any | None:
        """读取未过期的供应商缓存。"""

        ...

    def set_cache(self, provider: str, key: str, value: Any, ttl_seconds: int) -> None:
        """写入带过期时间的供应商缓存。"""

        ...


class DialogueRepository(Protocol):
    """旅行助手状态和消息幂等结果的持久化边界。"""

    def create_dialogue(self, state: TravelDialogueState) -> None:
        """创建一份新的旅行对话状态。"""

        ...

    def get_dialogue(self, session_id: UUID) -> TravelDialogueState | None:
        """读取旅行对话状态。"""

        ...

    def get_dialogue_response(
        self, session_id: UUID, message_id: UUID
    ) -> tuple[str, AssistantTurnResponse] | None:
        """读取一条已经处理过的消息及其响应。"""

        ...

    def list_memories(self) -> builtins.list[TravelMemory]:
        """读取用户明确保存的全部长期偏好。"""

        ...

    def delete_memory(self, key: MemorySlotName) -> bool:
        """删除一项长期偏好。"""

        ...

    def get_cache(self, provider: str, key: str) -> Any | None:
        """读取助手的短期结果缓存。"""

        ...

    def set_cache(self, provider: str, key: str, value: Any, ttl_seconds: int) -> None:
        """保存助手的短期结果缓存。"""

        ...

    def save_dialogue_response(
        self,
        state: TravelDialogueState,
        message_id: UUID,
        request_content: str,
        response: AssistantTurnResponse,
        memory_upserts: dict[MemorySlotName, str | builtins.list[str]] | None = None,
        memory_deletes: set[MemorySlotName] | None = None,
    ) -> None:
        """原子保存新状态、幂等响应和显式长期记忆变更。"""

        ...


class SqliteTripRepository:
    """基于标准库 sqlite3 的单用户行程仓库。"""

    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_tables()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """开启一个短事务，并在成功或异常退出时显式关闭连接。"""

        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _create_tables(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trips (
                    trip_id TEXT PRIMARY KEY,
                    destination TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    itinerary_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS planning_sessions (
                    session_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    session_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_cache (
                    provider TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (provider, cache_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS travel_dialogue_sessions (
                    session_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    active_flow TEXT,
                    state_json TEXT NOT NULL,
                    planning_session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS travel_dialogue_requests (
                    session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, message_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS travel_memories (
                    memory_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    source_session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def save(self, itinerary: Itinerary, request: TravelRequest) -> None:
        """保存完整快照；同一 ID 使用替换，便于后续支持重新生成。"""

        values = _trip_values(itinerary, request)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO trips
                (trip_id, destination, start_date, end_date, summary, created_at,
                 request_json, itinerary_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )

    def save_if_revision(
        self,
        itinerary: Itinerary,
        request: TravelRequest,
        expected_revision: int,
    ) -> bool:
        """在同一写事务内比较版本并更新，避免并发编辑互相覆盖。"""

        values = _trip_values(itinerary, request)
        with self._connect() as connection:
            # 版本字段在 JSON 内，不能依赖跨连接的“先读再写”。先取得写锁后再比较，
            # 使两个同时提交的编辑只有一个可以看到旧版本。
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT itinerary_json FROM trips WHERE trip_id = ?", (values[0],)
            ).fetchone()
            if row is None:
                return False
            stored = Itinerary.model_validate_json(row["itinerary_json"])
            if stored.revision != expected_revision:
                return False
            cursor = connection.execute(
                """
                UPDATE trips
                SET destination = ?, start_date = ?, end_date = ?, summary = ?,
                    created_at = ?, request_json = ?, itinerary_json = ?
                WHERE trip_id = ?
                """,
                (*values[1:], values[0]),
            )
        return cursor.rowcount == 1

    def get(self, trip_id: UUID) -> Itinerary | None:
        """读取已保存的完整行程，不存在时返回空值。"""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT itinerary_json FROM trips WHERE trip_id = ?", (str(trip_id),)
            ).fetchone()
        return Itinerary.model_validate_json(row["itinerary_json"]) if row else None

    def get_request(self, trip_id: UUID) -> TravelRequest | None:
        """读取请求快照，供局部编辑后重新计算预算使用。"""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_json FROM trips WHERE trip_id = ?", (str(trip_id),)
            ).fetchone()
        return TravelRequest.model_validate_json(row["request_json"]) if row else None

    def list(self) -> list[TripSummary]:
        """按创建时间倒序读取历史行程摘要。"""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trip_id, destination, start_date, end_date, summary, created_at
                FROM trips ORDER BY created_at DESC
                """
            ).fetchall()
        return [
            TripSummary(
                trip_id=UUID(row["trip_id"]),
                destination=row["destination"],
                start_date=datetime.fromisoformat(row["start_date"]).date(),
                end_date=datetime.fromisoformat(row["end_date"]).date(),
                summary=row["summary"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def delete(self, trip_id: UUID) -> bool:
        """删除指定行程，并返回是否实际删除。"""

        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM trips WHERE trip_id = ?", (str(trip_id),))
        return cursor.rowcount == 1

    def create_session(
        self, session: PlanningSession, idempotency_key: str | None = None
    ) -> PlanningSession:
        """原子创建会话；双击提交时由唯一键复用原任务。"""

        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO planning_sessions
                    (session_id, idempotency_key, status, request_json, session_json,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(session.session_id),
                        idempotency_key,
                        session.status,
                        session.request.model_dump_json(),
                        session.model_dump_json(),
                        session.created_at.isoformat(),
                        session.updated_at.isoformat(),
                    ),
                )
            return session
        except sqlite3.IntegrityError:
            existing = self._get_session_by_idempotency(idempotency_key)
            if existing is None:
                raise
            return existing

    def save_session(self, session: PlanningSession) -> None:
        """保存会话快照；步骤更新和最终状态使用同一数据源。"""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE planning_sessions
                SET status = ?, session_json = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    session.status,
                    session.model_dump_json(),
                    session.updated_at.isoformat(),
                    str(session.session_id),
                ),
            )

    def get_session(self, session_id: UUID) -> PlanningSession | None:
        """按 ID 读取完整会话快照。"""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT session_json FROM planning_sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
        return PlanningSession.model_validate_json(row["session_json"]) if row else None

    def _get_session_by_idempotency(self, key: str | None) -> PlanningSession | None:
        if not key:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT session_json FROM planning_sessions WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return PlanningSession.model_validate_json(row["session_json"]) if row else None

    def delete_session(self, session_id: UUID) -> bool:
        """删除一个尚未需要保留的规划会话。"""

        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM planning_sessions WHERE session_id = ?", (str(session_id),)
            )
        return cursor.rowcount == 1

    def list_recoverable_sessions(self) -> builtins.list[PlanningSession]:
        """读取中断在发现或生成阶段的会话，供启动时重新调度。"""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT session_json FROM planning_sessions
                WHERE status IN ('searching', 'generating')
                ORDER BY created_at
                """
            ).fetchall()
        return [PlanningSession.model_validate_json(row["session_json"]) for row in rows]

    def get_cache(self, provider: str, key: str) -> Any | None:
        """读取未过期缓存，并顺手删除过期记录。"""

        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json, expires_at FROM provider_cache
                WHERE provider = ? AND cache_key = ?
                """,
                (provider, key),
            ).fetchone()
            if row and datetime.fromisoformat(row["expires_at"]) <= now:
                connection.execute(
                    "DELETE FROM provider_cache WHERE provider = ? AND cache_key = ?",
                    (provider, key),
                )
                return None
        return json.loads(row["payload_json"]) if row else None

    def set_cache(self, provider: str, key: str, value: Any, ttl_seconds: int) -> None:
        """使用 SQLite 原子替换缓存，避免 JSON 文件并发覆盖。"""

        now = datetime.now(timezone.utc)
        expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds, timezone.utc)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO provider_cache
                (provider, cache_key, payload_json, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    key,
                    json.dumps(value, ensure_ascii=False),
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )

    def create_dialogue(self, state: TravelDialogueState) -> None:
        """创建旅行对话；会话 ID 冲突由调用方视为服务错误。"""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO travel_dialogue_sessions
                (session_id, revision, status, active_flow, state_json,
                 planning_session_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(state.session_id),
                    state.revision,
                    state.status,
                    state.active_flow,
                    state.model_dump_json(),
                    str(state.planning_session_id) if state.planning_session_id else None,
                    state.created_at.isoformat(),
                    state.updated_at.isoformat(),
                ),
            )

    def get_dialogue(self, session_id: UUID) -> TravelDialogueState | None:
        """按 ID 读取旅行对话的完整状态快照。"""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM travel_dialogue_sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
        return TravelDialogueState.model_validate_json(row["state_json"]) if row else None

    def get_dialogue_response(
        self, session_id: UUID, message_id: UUID
    ) -> tuple[str, AssistantTurnResponse] | None:
        """读取幂等响应，同时保留原消息以检测 message_id 冲突。"""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT response_json FROM travel_dialogue_requests
                WHERE session_id = ? AND message_id = ?
                """,
                (str(session_id), str(message_id)),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["response_json"])
        return str(payload["request_content"]), AssistantTurnResponse.model_validate(
            payload["response"]
        )

    def list_memories(self) -> builtins.list[TravelMemory]:
        """按键读取长期偏好；这里只保存用户明确要求记住的稳定字段。"""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM travel_memories ORDER BY memory_key"
            ).fetchall()
        return [
            TravelMemory(
                key=row["memory_key"],
                value=json.loads(row["value_json"]),
                version=row["version"],
                source_session_id=UUID(row["source_session_id"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def delete_memory(self, key: MemorySlotName) -> bool:
        """删除一项长期偏好；现有会话快照不会被静默改写。"""

        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM travel_memories WHERE memory_key = ?", (key,))
        return cursor.rowcount == 1

    def save_dialogue_response(
        self,
        state: TravelDialogueState,
        message_id: UUID,
        request_content: str,
        response: AssistantTurnResponse,
        memory_upserts: dict[MemorySlotName, str | builtins.list[str]] | None = None,
        memory_deletes: set[MemorySlotName] | None = None,
    ) -> None:
        """用乐观版本检查原子保存状态、响应和显式记忆变更。"""

        payload = json.dumps(
            {
                "request_content": request_content,
                "response": response.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE travel_dialogue_sessions
                SET revision = ?, status = ?, active_flow = ?, state_json = ?,
                    planning_session_id = ?, updated_at = ?
                WHERE session_id = ? AND revision = ?
                """,
                (
                    state.revision,
                    state.status,
                    state.active_flow,
                    state.model_dump_json(),
                    str(state.planning_session_id) if state.planning_session_id else None,
                    state.updated_at.isoformat(),
                    str(state.session_id),
                    state.revision - 1,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("dialogue revision conflict")
            connection.execute(
                """
                INSERT INTO travel_dialogue_requests
                (session_id, message_id, response_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(state.session_id),
                    str(message_id),
                    payload,
                    state.updated_at.isoformat(),
                ),
            )
            self._apply_memory_changes(
                connection,
                state,
                memory_upserts or {},
                memory_deletes or set(),
            )

    @staticmethod
    def _apply_memory_changes(
        connection: sqlite3.Connection,
        state: TravelDialogueState,
        upserts: dict[MemorySlotName, str | builtins.list[str]],
        deletes: set[MemorySlotName],
    ) -> None:
        """在消息事务内更新记忆，避免回复成功而偏好写入失败。"""

        for key in deletes:
            connection.execute("DELETE FROM travel_memories WHERE memory_key = ?", (key,))
        for key, value in upserts.items():
            connection.execute(
                """
                INSERT INTO travel_memories
                    (memory_key, value_json, version, source_session_id,
                     created_at, updated_at)
                VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT(memory_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    version = travel_memories.version + 1,
                    source_session_id = excluded.source_session_id,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    json.dumps(value, ensure_ascii=False),
                    str(state.session_id),
                    state.updated_at.isoformat(),
                    state.updated_at.isoformat(),
                ),
            )


def _trip_values(itinerary: Itinerary, request: TravelRequest) -> tuple[str, ...]:
    """统一生成行程表的持久化字段，避免普通保存与原子更新字段漂移。"""

    return (
        str(itinerary.trip_id),
        itinerary.destination,
        itinerary.start_date.isoformat(),
        itinerary.end_date.isoformat(),
        itinerary.summary,
        itinerary.created_at.isoformat(),
        request.model_dump_json(),
        itinerary.model_dump_json(),
    )


class CatalogRepository:
    """读取离线公开数据目录，不与行程历史数据库混用。"""

    SEARCH_RADIUS = 0.8

    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path)

    @property
    def available(self) -> bool:
        """判断目录是否已经由离线脚本生成。"""

        return self.database_path.is_file()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """打开只读目录事务，并保证查询结束后释放文件句柄。"""

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def resolve_city(self, destination: str) -> City:
        """按城市名或 GeoNames 中文别名查找城市坐标。"""

        if not self.available:
            raise LookupError("本地数据目录尚未生成")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT c.name, c.latitude, c.longitude
                FROM city_aliases a JOIN cities c ON c.city_id = a.city_id
                WHERE a.alias = ?
                ORDER BY a.population DESC
                LIMIT 1
                """,
                (destination.strip(),),
            ).fetchone()
        if row is None:
            raise LookupError(f"本地目录未覆盖城市：{destination}")
        return City(name=row["name"], latitude=row["latitude"], longitude=row["longitude"])

    def search_candidates(self, city: City) -> CandidateCatalog:
        """在城市中心附近读取三类 POI，供模型选择真实地点。"""

        if city.latitude is None or city.longitude is None:
            raise LookupError("本地城市缺少坐标")
        catalog = CandidateCatalog(
            attractions=self._search(city, "attraction", 12),
            restaurants=self._search(city, "restaurant", 12),
            hotels=self._search(city, "hotel", 8),
        )
        if not catalog.attractions:
            raise LookupError(f"本地目录没有找到城市附近的景点：{city.name}")
        return catalog

    def _search(self, city: City, category: str, limit: int) -> builtins.list[Poi]:
        latitude = city.latitude or 0
        longitude = city.longitude or 0
        radius = self.SEARCH_RADIUS
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT poi_id, name, address, category, latitude, longitude,
                       type_name, image_url
                FROM pois
                WHERE category = ?
                  AND latitude BETWEEN ? AND ?
                  AND longitude BETWEEN ? AND ?
                ORDER BY ((latitude - ?) * (latitude - ?)
                         + (longitude - ?) * (longitude - ?))
                LIMIT ?
                """,
                (
                    category,
                    latitude - radius,
                    latitude + radius,
                    longitude - radius,
                    longitude + radius,
                    latitude,
                    latitude,
                    longitude,
                    longitude,
                    limit,
                ),
            ).fetchall()
        return [
            Poi(
                id=row["poi_id"],
                name=row["name"],
                address=row["address"],
                category=row["category"],
                latitude=row["latitude"],
                longitude=row["longitude"],
                type_name=row["type_name"],
                image_url=row["image_url"],
            )
            for row in rows
        ]
