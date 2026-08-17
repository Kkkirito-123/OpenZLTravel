"""把旧 OpenZLTravel SQLite 一次性迁移到 PostgreSQL。

源文件始终以只读模式打开，迁移在单个 PostgreSQL 事务中完成。只有全部业务快照和
OpenZLAgent 对话记录都成功写入后，才登记源文件哈希并输出 24 小时认领码。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from psycopg import Connection, connect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import Settings

LEGACY_NAMESPACE = UUID("5927184c-e0db-51e5-890f-b852dc4c39a8")


def main() -> None:
    """解析命令行并执行一次性迁移。"""

    parser = argparse.ArgumentParser(description="迁移 OpenZLTravel SQLite 到 PostgreSQL")
    parser.add_argument("source", type=Path, help="旧 openzltravel.sqlite3 路径")
    parser.add_argument("--database-url", default=Settings().database_url)
    parser.add_argument("--claim-output", type=Path, help="把一次性认领码写入被忽略的本地文件")
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("缺少 DATABASE_URL")
    claim_token, counts = migrate_sqlite(args.source, args.database_url)
    print("迁移完成，各表数量：" + json.dumps(counts, ensure_ascii=False))
    if args.claim_output:
        _write_claim_token(args.claim_output, claim_token)
        print(f"一次性认领码已写入：{args.claim_output.resolve()}")
    else:
        print("一次性认领码（24 小时有效，请妥善保存）：" + claim_token)


def migrate_sqlite(source: Path, database_url: str) -> tuple[str, dict[str, int]]:
    """在一个事务中迁移源文件，并返回一次性认领码与导入统计。"""

    resolved = source.resolve(strict=True)
    source_hash = _file_sha256(resolved)
    legacy_id = uuid5(LEGACY_NAMESPACE, source_hash)
    claim_token = secrets.token_urlsafe(32)
    now = _now()
    counts: dict[str, int] = {}
    with closing(_open_readonly(resolved)) as sqlite_connection:
        with connect(database_url, row_factory=dict_row) as postgres:
            _ensure_not_imported(postgres, source_hash)
            _insert_legacy_visitor(postgres, legacy_id, source_hash, now)
            counts.update(_import_business_tables(sqlite_connection, postgres, legacy_id))
            counts.update(_import_conversation_tables(sqlite_connection, postgres))
            _insert_claim(postgres, legacy_id, claim_token, now)
            postgres.execute(
                """
                INSERT INTO app.importrecord
                    (importid, sourcehash, sourcefile, countsjson, importedat)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (uuid4(), source_hash, resolved.name, Jsonb(counts), now),
            )
    return claim_token, counts


def _import_business_tables(
    source: sqlite3.Connection,
    target: Connection[Any],
    visitor_id: UUID,
) -> dict[str, int]:
    importers: tuple[tuple[str, Callable[..., int]], ...] = (
        ("trips", _import_trips),
        ("planning_sessions", _import_planning_sessions),
        ("travel_dialogue_sessions", _import_dialogue_sessions),
        ("travel_dialogue_requests", _import_dialogue_requests),
        ("travel_memories", _import_memories),
    )
    counts = {
        table: importer(source, target, visitor_id) if _has_table(source, table) else 0
        for table, importer in importers
    }
    # Provider 缓存是可再生数据，Redis 阶段不把它混入 PostgreSQL 事务。
    counts["provider_cache_skipped"] = _table_count(source, "provider_cache")
    return counts


def _import_conversation_tables(
    source: sqlite3.Connection,
    target: Connection[Any],
) -> dict[str, int]:
    counts = {"conversation_turns": 0, "conversation_compactions": 0}
    if _has_table(source, "conversation_turns"):
        rows = source.execute("SELECT * FROM conversation_turns ORDER BY session_id, sequence")
        counts["conversation_turns"] = _execute_many(
            target,
            """
            INSERT INTO app.session_turns
                (session_id, sequence, user_content, assistant_content,
                 archived, created_at, metadata_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                (
                    row["session_id"],
                    row["sequence"],
                    row["user_content"],
                    row["assistant_content"],
                    bool(row["archived"]),
                    row["created_at"],
                    Jsonb(_json(row["metadata_json"])),
                )
                for row in rows
            ),
        )
    if _has_table(source, "conversation_compactions"):
        rows = source.execute("SELECT * FROM conversation_compactions ORDER BY created_at")
        counts["conversation_compactions"] = _execute_many(
            target,
            """
            INSERT INTO app.session_summaries
                (id, session_id, summarized_through_sequence, summary, reason,
                 source_turn_count, estimated_input_tokens, estimated_output_tokens,
                 created_at, metadata_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                (
                    row["id"],
                    row["session_id"],
                    row["summarized_through_sequence"],
                    row["summary"],
                    row["reason"],
                    row["source_turn_count"],
                    row["estimated_input_tokens"],
                    row["estimated_output_tokens"],
                    row["created_at"],
                    Jsonb(_json(row["metadata_json"])),
                )
                for row in rows
            ),
        )
    return counts


def _import_trips(
    source: sqlite3.Connection, target: Connection[Any], visitor_id: UUID
) -> int:
    rows = source.execute("SELECT * FROM trips ORDER BY created_at")
    return _execute_many(
        target,
        """
        INSERT INTO app.trip
            (tripid, visitorid, destination, startdate, enddate, summary,
             createdat, requestjson, itineraryjson, revision)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            (
                row["trip_id"],
                visitor_id,
                row["destination"],
                row["start_date"],
                row["end_date"],
                row["summary"],
                row["created_at"],
                Jsonb(_json(row["request_json"])),
                Jsonb(itinerary := _json(row["itinerary_json"])),
                int(itinerary.get("revision", 1)),
            )
            for row in rows
        ),
    )


def _import_planning_sessions(
    source: sqlite3.Connection, target: Connection[Any], visitor_id: UUID
) -> int:
    rows = source.execute("SELECT * FROM planning_sessions ORDER BY created_at")
    return _execute_many(
        target,
        """
        INSERT INTO app.planningsession
            (sessionid, visitorid, idempotencykey, status, requestjson,
             sessionjson, createdat, updatedat)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            (
                row["session_id"],
                visitor_id,
                row["idempotency_key"],
                row["status"],
                Jsonb(_json(row["request_json"])),
                Jsonb(_json(row["session_json"])),
                row["created_at"],
                row["updated_at"],
            )
            for row in rows
        ),
    )


def _import_dialogue_sessions(
    source: sqlite3.Connection, target: Connection[Any], visitor_id: UUID
) -> int:
    rows = source.execute("SELECT * FROM travel_dialogue_sessions ORDER BY created_at")
    return _execute_many(
        target,
        """
        INSERT INTO app.dialoguesession
            (sessionid, visitorid, revision, status, activeflow, statejson,
             planningsessionid, createdat, updatedat)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            (
                row["session_id"],
                visitor_id,
                row["revision"],
                row["status"],
                row["active_flow"],
                Jsonb(_json(row["state_json"])),
                row["planning_session_id"],
                row["created_at"],
                row["updated_at"],
            )
            for row in rows
        ),
    )


def _import_dialogue_requests(
    source: sqlite3.Connection, target: Connection[Any], visitor_id: UUID
) -> int:
    rows = source.execute("SELECT * FROM travel_dialogue_requests ORDER BY created_at")
    values = []
    for row in rows:
        payload = _json(row["response_json"])
        values.append(
            (
                row["session_id"],
                visitor_id,
                row["message_id"],
                str(payload["request_content"]),
                Jsonb(payload["response"]),
                row["created_at"],
            )
        )
    return _execute_many(
        target,
        """
        INSERT INTO app.dialoguerequest
            (sessionid, visitorid, messageid, requestcontent, responsejson, createdat)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        values,
    )


def _import_memories(
    source: sqlite3.Connection, target: Connection[Any], visitor_id: UUID
) -> int:
    rows = source.execute("SELECT * FROM travel_memories ORDER BY memory_key")
    return _execute_many(
        target,
        """
        INSERT INTO app.travelmemory
            (visitorid, memorykey, valuejson, version, sourcesessionid, createdat, updatedat)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            (
                visitor_id,
                row["memory_key"],
                Jsonb(_json(row["value_json"])),
                row["version"],
                row["source_session_id"],
                row["created_at"],
                row["updated_at"],
            )
            for row in rows
        ),
    )


def _insert_legacy_visitor(
    connection: Connection[Any], visitor_id: UUID, source_hash: str, now: datetime
) -> None:
    token_hash = hashlib.sha256(f"legacy:{source_hash}".encode()).hexdigest()
    connection.execute(
        """
        INSERT INTO app.visitor
            (visitorid, tokenhash, createdat, lastseenat, expiresat)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (visitor_id, token_hash, now, now, now + timedelta(days=3650)),
    )


def _insert_claim(
    connection: Connection[Any], visitor_id: UUID, token: str, now: datetime
) -> None:
    connection.execute(
        """
        INSERT INTO app.legacyclaim
            (claimid, visitorid, tokenhash, status, createdat, expiresat)
        VALUES (%s, %s, %s, 'pending', %s, %s)
        """,
        (uuid4(), visitor_id, _token_hash(token), now, now + timedelta(hours=24)),
    )


def _ensure_not_imported(connection: Connection[Any], source_hash: str) -> None:
    row = connection.execute(
        "SELECT 1 FROM app.importrecord WHERE sourcehash = %s",
        (source_hash,),
    ).fetchone()
    if row is not None:
        raise RuntimeError("这个 SQLite 文件已经迁移，拒绝重复导入")


def _execute_many(
    connection: Connection[Any], query: str, values: Iterable[tuple[Any, ...]]
) -> int:
    count = 0
    for value in values:
        connection.execute(query, value)
        count += 1
    return count


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    if not _has_table(connection, table):
        return 0
    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _json(value: str) -> Any:
    return json.loads(value)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_claim_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    main()
