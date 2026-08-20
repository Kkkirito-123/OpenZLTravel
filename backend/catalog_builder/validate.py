"""PostgreSQL 公共地点目录的完整性验证和中文查看命令。"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping
from typing import Any

from .geometry import normalize_name

SCHEMA_PATTERN = re.compile(r"^[a-z]+$")
COUNT_TABLES = (
    "source",
    "location",
    "locationsource",
    "region",
    "locationname",
    "geoplace",
    "poi",
    "boundary",
    "regionmatch",
)


def validate_catalog(
    connection: Any,
    schema: str = "catalog",
    *,
    require_full: bool = False,
) -> dict[str, int]:
    """验证行政树、坐标和来源关系，失败时抛出中文错误。"""

    _validate_schema(schema)
    checks = {
        "中国根节点数量": _scalar(
            connection,
            f"SELECT count(*) FROM {schema}.region WHERE level = 0 AND parentid IS NULL",
        ),
        "孤立行政节点数量": _scalar(
            connection,
            f"""
            SELECT count(*)
            FROM {schema}.region child
            LEFT JOIN {schema}.region parent ON parent.regionid = child.parentid
            WHERE child.level > 0 AND parent.regionid IS NULL
            """,
        ),
        "缺失行政实体数量": _scalar(
            connection,
            f"""
            SELECT count(*)
            FROM {schema}.location l
            LEFT JOIN {schema}.region r ON r.regionid = l.locationid
            WHERE l.kind = 'region' AND r.regionid IS NULL
            """,
        ),
        "错误树路径数量": _scalar(
            connection,
            f"""
            SELECT count(*)
            FROM {schema}.region child
            JOIN {schema}.region parent ON parent.regionid = child.parentid
            WHERE NOT (child.path <@ parent.path)
               OR nlevel(child.path) <> nlevel(parent.path) + 1
            """,
        ),
        "行政层级倒置数量": _scalar(
            connection,
            f"""
            SELECT count(*)
            FROM {schema}.region child
            JOIN {schema}.region parent ON parent.regionid = child.parentid
            WHERE child.level <= parent.level
            """,
        ),
        "非法边界数量": _scalar(
            connection,
            f"""
            SELECT count(*) FROM {schema}.boundary
            WHERE boundarygeom IS NOT NULL
              AND (ST_SRID(boundarygeom) <> 4326 OR NOT ST_IsValid(boundarygeom))
            """,
        ),
        "非法地点坐标数量": _scalar(
            connection,
            f"""
            SELECT count(*) FROM {schema}.location
            WHERE pointgeom IS NOT NULL AND ST_SRID(pointgeom) <> 4326
            """,
        ),
        "空检索名称数量": _scalar(
            connection,
            f"SELECT count(*) FROM {schema}.locationname WHERE normalizedname = ''",
        ),
        "缺失挂接结果数量": _scalar(
            connection,
            f"""
            SELECT count(*)
            FROM {schema}.location l
            LEFT JOIN {schema}.regionmatch m ON m.locationid = l.locationid
            WHERE l.kind IN ('place', 'poi') AND m.locationid IS NULL
            """,
        ),
    }
    failures = {
        "中国根节点数量": checks["中国根节点数量"] != 1,
        "孤立行政节点数量": checks["孤立行政节点数量"] != 0,
        "缺失行政实体数量": checks["缺失行政实体数量"] != 0,
        "错误树路径数量": checks["错误树路径数量"] != 0,
        "行政层级倒置数量": checks["行政层级倒置数量"] != 0,
        "非法边界数量": checks["非法边界数量"] != 0,
        "非法地点坐标数量": checks["非法地点坐标数量"] != 0,
        "空检索名称数量": checks["空检索名称数量"] != 0,
        "缺失挂接结果数量": checks["缺失挂接结果数量"] != 0,
    }
    failed = [name for name, is_failed in failures.items() if is_failed]
    counts = collect_counts(connection, schema)
    if require_full:
        _validate_full_counts(counts)
        _validate_xian_aliases(connection, schema)
    if failed:
        details = "，".join(f"{name}={checks[name]}" for name in failed)
        raise RuntimeError(f"地点目录完整性校验失败：{details}")
    return {**counts, **checks}


def collect_counts(connection: Any, schema: str = "catalog") -> dict[str, int]:
    """读取各核心表记录数和数据库占用。"""

    _validate_schema(schema)
    counts = {
        table: _scalar(connection, f"SELECT count(*) FROM {schema}.{table}")
        for table in COUNT_TABLES
    }
    counts["unmatched"] = _scalar(
        connection,
        f"SELECT count(*) FROM {schema}.regionmatch WHERE matchmethod = 'unmatched'",
    )
    counts["missingboundarygeometry"] = _scalar(
        connection,
        f"""
        SELECT count(*) FROM {schema}.boundary
        WHERE centergeom IS NULL AND boundarygeom IS NULL
        """,
    )
    counts["indexbytes"] = _scalar(
        connection,
        """
        SELECT coalesce(sum(pg_indexes_size(c.oid)), 0)::bigint
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind IN ('r', 'm')
        """,
        (schema,),
    )
    counts["sizebytes"] = _scalar(
        connection,
        """
        SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0)::bigint
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind IN ('r', 'm')
        """,
        (schema,),
    )
    latest = connection.execute(
        f"""
        SELECT counts FROM {schema}.build
        WHERE status = 'completed'
        ORDER BY finishedat DESC NULLS LAST
        LIMIT 1
        """
    ).fetchone()
    if latest and isinstance(latest[0], dict):
        for key in ("invalidboundary", "missingboundarygeometry", "buildseconds"):
            if key in latest[0]:
                counts[key] = int(latest[0][key])
    return counts


def print_stats(stats: Mapping[str, int]) -> None:
    """使用中文输出构建统计，方便不熟悉 SQL 的用户检查。"""

    labels = {
        "source": "数据来源",
        "location": "统一地点",
        "locationsource": "来源映射",
        "region": "行政区节点",
        "locationname": "可检索名称",
        "geoplace": "GeoNames 地名",
        "poi": "旅行 POI",
        "boundary": "行政区边界",
        "regionmatch": "行政区挂接",
        "unmatched": "未挂接地点",
        "invalidboundary": "无法解析的边界",
        "missingboundarygeometry": "来源未提供几何的边界",
        "buildseconds": "构建耗时（秒）",
    }
    for key, label in labels.items():
        if key in stats:
            print(f"{label}：{stats[key]:,}")
    if "sizebytes" in stats:
        print(f"数据库表和索引占用：{stats['sizebytes'] / 1024 / 1024:.2f} MiB")
    if "indexbytes" in stats:
        print(f"其中索引占用：{stats['indexbytes'] / 1024 / 1024:.2f} MiB")
    matched_total = stats.get("regionmatch", 0)
    if matched_total:
        matched = matched_total - stats.get("unmatched", 0)
        print(f"行政区匹配率：{matched / matched_total:.2%}")


def print_tree(connection: Any, query: str, depth: int = 2, schema: str = "catalog") -> None:
    """按中文名称定位行政节点并打印有限深度的树。"""

    _validate_schema(schema)
    normalized = normalize_name(query)
    row = connection.execute(
        f"""
        SELECT r.regionid, r.path, r.level, r.name
        FROM {schema}.locationname n
        JOIN {schema}.region r ON r.regionid = n.locationid
        WHERE n.normalizedname = %s
        ORDER BY (r.status = 'current') DESC, n.priority DESC, r.level ASC
        LIMIT 1
        """,
        (normalized,),
    ).fetchone()
    if row is None:
        raise LookupError(f"没有找到行政区：{query}")
    base_level = int(row[2])
    rows = connection.execute(
        f"""
        SELECT name, level, status
        FROM {schema}.region
        WHERE path <@ %s::ltree AND level <= %s
        ORDER BY path
        """,
        (str(row[1]), base_level + max(0, depth)),
    ).fetchall()
    for name, level, status in rows:
        suffix = "（历史）" if status == "legacy" else ""
        print(f"{'  ' * (int(level) - base_level)}{name}{suffix}")


def main() -> None:
    """提供 verify、stats 和 tree 三个只读命令。"""

    parser = argparse.ArgumentParser(description="验证和查看 OpenZLTravel 公共地点库")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify", help="执行完整性校验")
    subparsers.add_parser("stats", help="查看记录数和数据库大小")
    tree = subparsers.add_parser("tree", help="按名称查看行政树")
    tree.add_argument("name")
    tree.add_argument("--depth", type=int, default=2)
    arguments = parser.parse_args()
    database_url = os.getenv("CATALOG_DATABASE_URL")
    if not database_url:
        raise SystemExit("缺少 CATALOG_DATABASE_URL，请使用 catalog.ps1 运行")
    try:
        import psycopg
    except ImportError as error:
        raise SystemExit("缺少 psycopg，请先执行：python -m pip install -e '.[catalog]'") from error
    with psycopg.connect(database_url) as connection:
        if arguments.command == "verify":
            print_stats(validate_catalog(connection, require_full=True))
            print("地点目录完整性校验通过。")
        elif arguments.command == "stats":
            print_stats(collect_counts(connection))
        else:
            print_tree(connection, arguments.name, arguments.depth)


def _scalar(connection: Any, query: str, parameters: tuple[Any, ...] = ()) -> int:
    row = connection.execute(query, parameters).fetchone()
    return int(row[0]) if row else 0


def _validate_schema(schema: str) -> None:
    if not SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError(f"非法 Schema 名称：{schema}")


def _validate_full_counts(counts: Mapping[str, int]) -> None:
    minimums = {
        "source": 5,
        "region": 665_000,
        "geoplace": 959_000,
        "boundary": 3_500,
        "poi": 1,
    }
    missing = [
        f"{name}={counts[name]}<{minimum}"
        for name, minimum in minimums.items()
        if counts[name] < minimum
    ]
    if missing:
        raise RuntimeError(f"全量数据记录数不足：{json.dumps(missing, ensure_ascii=False)}")


def _validate_xian_aliases(connection: Any, schema: str) -> None:
    """确认西安的官方名、简称和拼音都指向同一行政节点。"""

    rows = connection.execute(
        f"""
        SELECT n.normalizedname
        FROM {schema}.region r
        JOIN {schema}.locationname n ON n.locationid = r.regionid
        WHERE r.adcode = '610100000000'
          AND n.normalizedname IN ('西安市', '西安', 'xian')
        """
    ).fetchall()
    aliases = {str(row[0]) for row in rows}
    expected = {"西安市", "西安", "xian"}
    if aliases != expected:
        missing = ", ".join(sorted(expected - aliases))
        raise RuntimeError(f"西安市检索别名不完整：{missing}")


if __name__ == "__main__":
    main()
