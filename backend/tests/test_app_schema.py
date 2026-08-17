"""业务 PostgreSQL Schema 与生产 SQLite 清理门禁。"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (BACKEND_ROOT / "database" / "app.sql").read_text(encoding="utf-8")
CUSTOM_TABLES = {
    "schemaversion",
    "visitor",
    "trip",
    "planningsession",
    "dialoguesession",
    "dialoguerequest",
    "travelmemory",
    "legacyclaim",
    "importrecord",
}
OPENZL_TABLES = {"session_turns", "session_summaries"}


def test_app_schema_contains_all_shared_state_tables() -> None:
    """业务 Schema 必须覆盖匿名身份、任务、行程、记忆和迁移审计。"""

    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS app\.([a-z0-9_]+)", SCHEMA))
    assert tables == CUSTOM_TABLES | OPENZL_TABLES
    assert "UNIQUE (visitorid, idempotencykey)" in SCHEMA
    assert "DROP TABLE IF EXISTS app.providercache" in SCHEMA
    assert "ON DELETE CASCADE ON UPDATE CASCADE" in SCHEMA


def test_custom_database_identifiers_have_no_underscores() -> None:
    """除 OpenZLAgent 固定兼容表外，自定义表、字段和索引不使用下划线。"""

    for table in CUSTOM_TABLES:
        assert "_" not in table
        for column in _table_columns(table):
            assert "_" not in column
    indexes = re.findall(r"CREATE INDEX IF NOT EXISTS ([a-z0-9_]+)", SCHEMA)
    assert all("_" not in name for name in indexes if name != "session_turns_hot_idx")


def test_every_app_table_and_column_has_a_chinese_comment() -> None:
    """中文备注让新手可以直接从数据库工具理解字段用途。"""

    for table in CUSTOM_TABLES | OPENZL_TABLES:
        table_comment = _comment(rf"COMMENT ON TABLE app\.{table} IS '([^']+)'", SCHEMA)
        assert _has_chinese(table_comment)
        for column in _table_columns(table):
            column_comment = _comment(
                rf"COMMENT ON COLUMN app\.{table}\.{column} IS '([^']+)'",
                SCHEMA,
            )
            assert _has_chinese(column_comment), f"{table}.{column} 缺少中文备注"


def test_production_app_no_longer_imports_sqlite() -> None:
    """SQLite 只能留在迁移工具和测试中，不能回到生产运行路径。"""

    app_root = BACKEND_ROOT / "app"
    contents = "\n".join(path.read_text(encoding="utf-8") for path in app_root.rglob("*.py"))
    assert "import sqlite3" not in contents
    assert "SqliteTripRepository" not in contents
    assert "DATABASE_PATH" not in contents
    assert "CATALOG_PATH" not in contents


def _table_columns(table: str) -> list[str]:
    body = _comment(
        rf"CREATE TABLE IF NOT EXISTS app\.{table} \((.*?)\n\);",
        SCHEMA,
        flags=re.DOTALL,
    )
    return re.findall(r"^    ([a-z][a-z0-9_]*)\s+[A-Z]", body, flags=re.MULTILINE)


def _comment(pattern: str, text: str, flags: int = 0) -> str:
    match = re.search(pattern, text, flags)
    assert match is not None, pattern
    return match.group(1)


def _has_chinese(value: str) -> bool:
    return any("\u4e00" <= character <= "\u9fff" for character in value)
