"""PostgreSQL 地点目录 Schema 静态契约测试。"""

from __future__ import annotations

import re
from pathlib import Path

SCHEMA_FILE = Path(__file__).parents[1] / "catalog_builder" / "schema.sql"
EXPECTED_TABLES = {
    "source",
    "build",
    "location",
    "locationsource",
    "region",
    "locationname",
    "geoplace",
    "poi",
    "boundary",
    "regionmatch",
}


def test_schema_uses_required_extensions_and_tables() -> None:
    """Schema 必须包含空间、树形、模糊检索扩展及十张核心表。"""

    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    extensions = set(re.findall(r"CREATE EXTENSION IF NOT EXISTS ([a-z0-9_]+)", sql))
    tables = set(re.findall(r"CREATE TABLE __schema__\.([a-z0-9_]+)", sql))

    assert extensions == {"postgis", "ltree", "pg_trgm"}
    assert tables == EXPECTED_TABLES


def test_custom_database_identifiers_have_no_underscores() -> None:
    """自定义表、字段和索引名必须遵循小写无下划线规则。"""

    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    tables = re.findall(r"CREATE TABLE __schema__\.([a-z0-9_]+)", sql)
    indexes = re.findall(r"CREATE INDEX ([a-z0-9_]+)", sql)
    columns = re.findall(r"COMMENT ON COLUMN __schema__\.[a-z]+\.([a-z0-9_]+)", sql)

    assert all("_" not in name for name in tables + indexes + columns)


def test_every_table_and_column_has_a_chinese_comment() -> None:
    """数据库对象必须能由中文文档和 COMMENT 直接解释。"""

    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    table_comments = set(
        re.findall(
            r"COMMENT ON TABLE __schema__\.([a-z]+) IS '[^']*[\u3400-\u9fff]",
            sql,
        )
    )
    column_comments = {
        (table, column)
        for table, column in re.findall(
            r"COMMENT ON COLUMN __schema__\.([a-z]+)\.([a-z0-9]+) IS '[^']*[\u3400-\u9fff]",
            sql,
        )
    }
    declared_columns: set[tuple[str, str]] = set()
    for table, body in re.findall(
        r"CREATE TABLE __schema__\.([a-z]+) \((.*?)\n\);",
        sql,
        flags=re.DOTALL,
    ):
        for line in body.splitlines():
            match = re.match(r"\s{4}([a-z][a-z0-9]*)\s", line)
            if match:
                declared_columns.add((table, match.group(1)))

    assert table_comments == EXPECTED_TABLES
    assert column_comments == declared_columns
