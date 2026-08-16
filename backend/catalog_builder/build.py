"""构建并原子发布 PostgreSQL 公共地点目录。

构建过程只读取 backend/data/raw。所有大数据先进入 catalogbuild，完整性验证通过后
才替换正式 catalog，因此失败不会留下半成品供运行时读取。
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir, mkdtemp
from typing import Any
from uuid import uuid4

from .geometry import normalize_name
from .sources import (
    BoundaryRaw,
    GeoRaw,
    PoiRaw,
    RegionRaw,
    iter_areacity_regions,
    iter_boundaries,
    iter_geonames,
    iter_modood_regions,
    iter_osm_pois,
    names_json,
    root_region,
    source_definitions,
)
from .validate import print_stats, validate_catalog

BUILD_SCHEMA = "catalogbuild"
SCHEMA_TOKEN = "__schema__"
INDEX_MARKER = "-- catalogindexes"
SCHEMA_PATTERN = re.compile(r"^[a-z]+$")
OSM_TEMP_PREFIX = "openzltravel-osm-"
OSM_CLEANUP_DELAYS = (0.0, 0.1, 0.5, 1.0)


@dataclass(frozen=True, slots=True)
class BuildPaths:
    """全量目录构建所需的本地只读输入路径。"""

    modooddir: Path
    modoodarchive: Path
    arearegions: Path
    areaboundaries: Path
    geonames: Path
    osm: Path

    @classmethod
    def defaults(cls) -> BuildPaths:
        """返回项目当前下载目录的默认输入路径。"""

        backend = Path(__file__).resolve().parents[1]
        admin = backend / "data" / "raw" / "administrative"
        modood = admin / "modood-2.7.0"
        area = admin / "areacity-2025.251231.260403"
        return cls(
            modooddir=modood / "source" / "dist",
            modoodarchive=modood / "china-division-2.7.0.tgz",
            arearegions=area / "source" / "ok_data_level4.csv",
            areaboundaries=area / "source" / "ok_geo.csv",
            geonames=backend / "data" / "raw" / "geonames" / "CN.zip",
            osm=backend / "data" / "raw" / "osm" / "china-latest.osm.pbf",
        )

    def required(self) -> tuple[Path, ...]:
        """列出构建前必须存在的文件。"""

        return (
            self.modooddir / "provinces.csv",
            self.modooddir / "cities.csv",
            self.modooddir / "areas.csv",
            self.modooddir / "streets.csv",
            self.modooddir / "villages.csv",
            self.modoodarchive,
            self.arearegions,
            self.areaboundaries,
            self.geonames,
            self.osm,
        )


class CatalogBuilder:
    """以流式 COPY 构建、校验并发布一代地点目录。"""

    def __init__(
        self,
        connection: Any,
        paths: BuildPaths,
        *,
        require_full: bool = True,
    ) -> None:
        self.connection = connection
        self.paths = paths
        self.require_full = require_full
        self.buildid = uuid4()
        self.started = time.monotonic()
        self.sources: dict[str, int] = {}
        self.metrics: dict[str, int] = {}
        self.boundaryerrors: list[dict[str, str]] = []
        self.indexsql = ""
        self.locked = False

    def run(self) -> dict[str, int]:
        """执行完整构建；任何错误都会阻止正式 Schema 切换。"""

        self._lock()
        try:
            self._prepare_schema()
            self._start_build()
            self._import_sources()
            self._import_regions()
            self._import_boundaries()
            self._import_geonames()
            self._import_osm()
            self._create_indexes()
            self._match_regions()
            stats = validate_catalog(
                self.connection,
                BUILD_SCHEMA,
                require_full=self.require_full,
            )
            stats.update(self.metrics)
            stats["buildseconds"] = round(time.monotonic() - self.started)
            self._finish_build(stats)
            self._publish()
            self._progress("正式地点目录已发布")
            return stats
        except Exception as error:
            self._record_failure(error)
            raise
        finally:
            self._unlock()

    def _lock(self) -> None:
        row = self.connection.execute(
            "SELECT pg_try_advisory_lock(hashtext('openzltravelcatalogbuild'))"
        ).fetchone()
        if not row or not row[0]:
            raise RuntimeError("已有地点目录构建任务正在运行")
        self.locked = True

    def _unlock(self) -> None:
        if not self.locked:
            return
        try:
            self.connection.execute(
                "SELECT pg_advisory_unlock(hashtext('openzltravelcatalogbuild'))"
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
        finally:
            self.locked = False

    def _prepare_schema(self) -> None:
        _ensure_reader_role(self.connection)
        self.connection.execute(f"DROP SCHEMA IF EXISTS {BUILD_SCHEMA} CASCADE")
        schema_sql = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        definition, marker, indexes = schema_sql.partition(INDEX_MARKER)
        if not marker:
            raise RuntimeError("schema.sql 缺少索引分隔标记")
        self.connection.execute(definition.replace(SCHEMA_TOKEN, BUILD_SCHEMA))
        self.indexsql = indexes.replace(SCHEMA_TOKEN, BUILD_SCHEMA)
        self.connection.commit()
        self._progress("PostGIS Schema 已创建")

    def _start_build(self) -> None:
        self.connection.execute(
            f"""
            INSERT INTO {BUILD_SCHEMA}.build(buildid, status, startedat)
            VALUES (%s, 'running', %s)
            """,
            (self.buildid, datetime.now(timezone.utc)),
        )
        self.connection.commit()

    def _import_sources(self) -> None:
        definitions = source_definitions(
            self.paths.modoodarchive,
            (self.paths.arearegions, self.paths.areaboundaries),
            self.paths.geonames,
            self.paths.osm,
        )
        for source in definitions:
            row = self.connection.execute(
                f"""
                INSERT INTO {BUILD_SCHEMA}.source(
                    sourcecode, namezh, version, sourceurl, licensename,
                    licenseurl, checksumsha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING sourceid
                """,
                (
                    source.code,
                    source.name,
                    source.version,
                    source.url,
                    source.license_name,
                    source.license_url,
                    source.checksum,
                ),
            ).fetchone()
            self.sources[source.code] = int(row[0])
        self.connection.commit()
        self._progress("数据来源和许可证已登记")

    def _import_regions(self) -> None:
        self.connection.execute(_region_raw_table_sql())
        rows = _chain_regions(self.paths)
        copied = _copy_rows(self.connection, _region_copy_sql(), map(_region_row, rows))
        self.connection.execute(_region_insert_sql())
        self.connection.execute(_region_source_sql())
        self.connection.execute(_region_name_sql())
        self.connection.execute("DROP TABLE regionraw")
        self.connection.commit()
        self._progress(f"行政区来源记录已合并：{copied:,}")

    def _import_boundaries(self) -> None:
        self.connection.execute(_boundary_raw_table_sql())
        invalid = 0
        missing_geometry = 0

        def rows() -> Iterator[tuple[Any, ...]]:
            nonlocal invalid, missing_geometry
            for record in iter_boundaries(self.paths.areaboundaries):
                invalid += int(record.error is not None)
                if record.error:
                    self.boundaryerrors.append(
                        {
                            "adcode": record.adcode,
                            "sourcekey": record.sourcekey,
                            "error": record.error,
                        }
                    )
                elif record.center is None and record.polygon is None:
                    missing_geometry += 1
                    self.boundaryerrors.append(
                        {
                            "adcode": record.adcode,
                            "sourcekey": record.sourcekey,
                            "error": "来源未提供中心点和边界几何",
                        }
                    )
                yield _boundary_row(record)

        copied = _copy_rows(self.connection, _boundary_copy_sql(), rows())
        self.metrics["invalidboundary"] = invalid
        self.metrics["missingboundarygeometry"] = missing_geometry
        self.connection.execute(_boundary_insert_sql(), (self.sources["areacity"],))
        self.connection.execute("DROP TABLE boundaryraw")
        self.connection.commit()
        self._progress(
            f"行政边界已处理：{copied:,}，损坏记录：{invalid:,}，缺少几何：{missing_geometry:,}"
        )

    def _import_geonames(self) -> None:
        self.connection.execute(_geo_raw_table_sql())
        copied = _copy_rows(
            self.connection,
            _geo_copy_sql(),
            map(_geo_row, iter_geonames(self.paths.geonames)),
        )
        sourceid = self.sources["geonames"]
        self.connection.execute(_geo_location_sql())
        self.connection.execute(_geo_source_sql(), (sourceid,))
        self.connection.execute(_geoplace_sql())
        self.connection.execute(_geo_name_sql(), (sourceid,))
        self.connection.execute("DROP TABLE georaw")
        self.connection.commit()
        self._progress(f"GeoNames 已导入：{copied:,}")

    def _import_osm(self) -> None:
        self.connection.execute(_poi_raw_table_sql())
        tempdir = Path(mkdtemp(prefix=OSM_TEMP_PREFIX))
        try:
            records = iter_osm_pois(self.paths.osm, tempdir / "nodes.cache")
            try:
                copied = _copy_rows(
                    self.connection,
                    _poi_copy_sql(),
                    map(_poi_row, records),
                )
            finally:
                # pyosmium 的磁盘索引由 C++ 对象持有。Windows 必须先释放迭代器，
                # 否则紧接着删除 nodes.cache 会触发 WinError 32。
                _close_iterator(records)
                del records
                gc.collect()
        finally:
            if not _remove_osm_tempdir(tempdir):
                scheduled = _schedule_osm_tempdir_cleanup(tempdir)
                state = "已安排进程退出后清理" if scheduled else "请在进程退出后手动清理"
                self._progress(f"OSM 临时索引仍被系统占用，{state}：{tempdir}")
        sourceid = self.sources["osm"]
        self.connection.execute(_poi_location_sql())
        self.connection.execute(_poi_source_sql(), (sourceid,))
        self.connection.execute(_poi_insert_sql())
        self.connection.execute(_poi_name_sql(), (sourceid,))
        self.connection.execute("DROP TABLE poiraw")
        self.connection.commit()
        self._progress(f"OSM 旅行 POI 已导入：{copied:,}")

    def _create_indexes(self) -> None:
        self.connection.execute(self.indexsql)
        self.connection.execute(f"ANALYZE {BUILD_SCHEMA}.boundary")
        self.connection.execute(f"ANALYZE {BUILD_SCHEMA}.location")
        self.connection.commit()
        self._progress("全文、树形和空间索引已创建")

    def _match_regions(self) -> None:
        self.connection.execute(_source_code_match_sql())
        self.connection.execute(_spatial_match_sql())
        self.connection.execute(_unmatched_sql())
        self.connection.execute(f"ANALYZE {BUILD_SCHEMA}.regionmatch")
        self.connection.commit()
        self._progress("GeoNames 与 OSM 已挂接行政树")

    def _finish_build(self, stats: dict[str, int]) -> None:
        from psycopg.types.json import Jsonb

        self.connection.execute(
            f"""
            UPDATE {BUILD_SCHEMA}.build
            SET status = 'completed', finishedat = %s, counts = %s, details = %s
            WHERE buildid = %s
            """,
            (
                datetime.now(timezone.utc),
                Jsonb(stats),
                Jsonb({"boundaryerrors": self.boundaryerrors}),
                self.buildid,
            ),
        )
        self.connection.commit()

    def _publish(self) -> None:
        self.connection.execute("DROP SCHEMA IF EXISTS catalogprevious CASCADE")
        if _schema_exists(self.connection, "catalog"):
            self.connection.execute("ALTER SCHEMA catalog RENAME TO catalogprevious")
        self.connection.execute(f"ALTER SCHEMA {BUILD_SCHEMA} RENAME TO catalog")
        self.connection.commit()

    def _record_failure(self, error: Exception) -> None:
        self.connection.rollback()
        if not _schema_exists(self.connection, BUILD_SCHEMA):
            return
        try:
            self.connection.execute(
                f"""
                UPDATE {BUILD_SCHEMA}.build
                SET status = 'failed', finishedat = %s,
                    details = %s::jsonb, errormessage = %s
                WHERE buildid = %s
                """,
                (
                    datetime.now(timezone.utc),
                    json.dumps(
                        {"boundaryerrors": self.boundaryerrors},
                        ensure_ascii=False,
                    ),
                    str(error)[:1000],
                    self.buildid,
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()

    def _progress(self, message: str) -> None:
        elapsed = time.monotonic() - self.started
        print(f"[{elapsed:8.1f}s] {message}", flush=True)


def main() -> None:
    """检查输入和连接配置后执行全量构建。"""

    parser = argparse.ArgumentParser(description="构建 OpenZLTravel PostgreSQL 公共地点库")
    parser.parse_args()
    database_url = os.getenv("CATALOG_DATABASE_URL")
    if not database_url:
        raise SystemExit("缺少 CATALOG_DATABASE_URL，请使用 catalog.ps1 运行")
    paths = BuildPaths.defaults()
    missing = [str(path) for path in paths.required() if not path.exists()]
    if missing:
        raise SystemExit(f"缺少原始数据：{json.dumps(missing, ensure_ascii=False)}")
    try:
        import psycopg
    except ImportError as error:
        raise SystemExit("缺少 psycopg，请先安装 requirements-data.txt") from error
    with psycopg.connect(database_url) as connection:
        stats = CatalogBuilder(connection, paths).run()
    print_stats(stats)


def _ensure_reader_role(connection: Any) -> None:
    connection.execute(
        """
        DO $role$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'catalogreader') THEN
                CREATE ROLE catalogreader NOLOGIN;
            END IF;
        END
        $role$;
        """
    )
    connection.commit()


def _schema_exists(connection: Any, schema: str) -> bool:
    if not SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError(f"非法 Schema 名称：{schema}")
    row = connection.execute(
        "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = %s)", (schema,)
    ).fetchone()
    return bool(row and row[0])


def _copy_rows(connection: Any, statement: str, rows: Iterable[tuple[Any, ...]]) -> int:
    count = 0
    with connection.cursor().copy(statement) as copy:
        for row in rows:
            copy.write_row(row)
            count += 1
    return count


def _close_iterator(records: Iterator[Any]) -> None:
    """显式关闭可能持有本地文件句柄的流式迭代器。"""

    close = getattr(records, "close", None)
    if callable(close):
        close()


def _remove_osm_tempdir(path: Path) -> bool:
    """在 Windows 文件映射延迟释放时，有限重试清理 OSM 临时索引。"""

    resolved = path.resolve()
    temp_root = Path(gettempdir()).resolve()
    if resolved.parent != temp_root or not resolved.name.startswith(OSM_TEMP_PREFIX):
        raise RuntimeError(f"拒绝清理非 OSM 临时目录：{resolved}")

    for delay in OSM_CLEANUP_DELAYS:
        if delay:
            time.sleep(delay)
        gc.collect()
        try:
            shutil.rmtree(resolved)
        except FileNotFoundError:
            return True
        except PermissionError:
            continue
        return True
    return not resolved.exists()


def _schedule_osm_tempdir_cleanup(path: Path) -> bool:
    """为 Windows 的进程级文件锁安排有限重试，不阻断已完成构建。"""

    resolved = path.resolve()
    temp_root = Path(gettempdir()).resolve()
    if resolved.parent != temp_root or not resolved.name.startswith(OSM_TEMP_PREFIX):
        raise RuntimeError(f"拒绝安排清理非 OSM 临时目录：{resolved}")
    if os.name != "nt":
        return False

    escaped = str(resolved).replace("'", "''")
    script = (
        "$attempt=0; "
        f"while ($attempt -lt 80 -and (Test-Path -LiteralPath '{escaped}')) "
        "{ "
        f"Remove-Item -LiteralPath '{escaped}' -Recurse -Force -ErrorAction SilentlyContinue; "
        "$attempt++; "
        f"if (Test-Path -LiteralPath '{escaped}') "
        "{ Start-Sleep -Milliseconds 250 } "
        "}"
    )
    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                script,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # CREATE_NO_WINDOW 足以隐藏控制台；DETACHED_PROCESS 会让 PowerShell
            # 在部分 Windows 环境中直接退出，导致清理命令根本没有执行。
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
    except OSError:
        return False
    return True


def _chain_regions(paths: BuildPaths) -> Iterator[RegionRaw]:
    yield root_region()
    yield from iter_modood_regions(paths.modooddir)
    yield from iter_areacity_regions(paths.arearegions)


def _region_row(record: RegionRaw) -> tuple[Any, ...]:
    return (
        record.regionid,
        record.sourcecode,
        record.sourcekey,
        record.adcode,
        record.parentid,
        record.level,
        record.path,
        record.name,
        normalize_name(record.name),
        record.shortname,
        normalize_name(record.shortname),
        record.pinyin,
        normalize_name(record.pinyin),
        record.initial,
        record.status,
        record.priority,
    )


def _geo_row(record: GeoRaw) -> tuple[Any, ...]:
    return (
        record.locationid,
        record.geonameid,
        record.canonicalname,
        record.point,
        record.importance,
        record.asciiname,
        record.featureclass,
        record.featurecode,
        record.countrycode,
        record.admin1,
        record.admin2,
        record.admin3,
        record.admin4,
        record.population,
        record.elevation,
        record.dem,
        record.timezone,
        record.modifiedon,
        names_json(record.names),
    )


def _boundary_row(record: BoundaryRaw) -> tuple[Any, ...]:
    return record.adcode, record.sourcekey, record.center, record.polygon, record.error


def _poi_row(record: PoiRaw) -> tuple[Any, ...]:
    return (
        record.locationid,
        record.elementtype,
        record.elementid,
        record.canonicalname,
        record.category,
        record.typename,
        record.address,
        record.imageurl,
        record.sourceadcode,
        record.sourceurl,
        record.point,
        record.areawkb,
        names_json(record.names),
    )


def _region_raw_table_sql() -> str:
    return """
    CREATE TEMP TABLE regionraw (
        regionid UUID, sourcecode TEXT, sourcekey TEXT, adcode CHAR(12), parentid UUID,
        level SMALLINT, path LTREE, name TEXT, namenorm TEXT, shortname TEXT,
        shortnorm TEXT, pinyin TEXT, pinyinnorm TEXT, initial TEXT, status TEXT, priority INTEGER
    ) ON COMMIT PRESERVE ROWS
    """


def _region_copy_sql() -> str:
    return """
    COPY regionraw(
        regionid, sourcecode, sourcekey, adcode, parentid, level, path, name, namenorm,
        shortname, shortnorm, pinyin, pinyinnorm, initial, status, priority
    ) FROM STDIN
    """


def _region_insert_sql() -> str:
    return f"""
    WITH ranked AS (
        SELECT r.*, row_number() OVER (
            PARTITION BY adcode
            ORDER BY priority DESC,
                     (parentid IS DISTINCT FROM regionid) DESC,
                     sourcecode, path
        ) AS rowrank
        FROM regionraw r
    ), chosen AS (
        SELECT * FROM ranked WHERE rowrank = 1
    )
    INSERT INTO {BUILD_SCHEMA}.location(locationid, kind, canonicalname)
    SELECT regionid, 'region', name FROM chosen;

    WITH RECURSIVE ranked AS (
        SELECT r.*, row_number() OVER (
            PARTITION BY adcode
            ORDER BY priority DESC,
                     (parentid IS DISTINCT FROM regionid) DESC,
                     sourcecode, path
        ) AS rowrank
        FROM regionraw r
    ), chosen AS (
        SELECT * FROM ranked WHERE rowrank = 1
    ), tree AS (
        SELECT c.*, 'cn'::ltree AS treepath
        FROM chosen c
        WHERE c.parentid IS NULL
        UNION ALL
        SELECT c.*, parent.treepath || text2ltree('r' || c.adcode::text)
        FROM chosen c
        JOIN tree parent ON parent.regionid = c.parentid
    )
    INSERT INTO {BUILD_SCHEMA}.region(
        regionid, adcode, parentid, level, path, name, shortname,
        pinyin, pinyininitial, status
    )
    SELECT regionid, adcode, parentid, level, treepath, name, shortname,
           pinyin, initial, status
    FROM tree;
    """


def _region_source_sql() -> str:
    return f"""
    WITH ranked AS (
        SELECT r.*,
               row_number() OVER (
                   PARTITION BY adcode
                   ORDER BY priority DESC,
                            (parentid IS DISTINCT FROM regionid) DESC,
                            sourcecode, path
               ) = 1 AS primaryrow
        FROM regionraw r
    ), chosen AS (
        SELECT DISTINCT ON (sourcecode, sourcekey) *
        FROM ranked
        ORDER BY sourcecode, sourcekey, primaryrow DESC, priority DESC, path
    )
    INSERT INTO {BUILD_SCHEMA}.locationsource(locationid, sourceid, sourcekey, rawname, isprimary)
    SELECT r.regionid, s.sourceid, r.sourcekey, r.name, r.primaryrow
    FROM chosen r
    JOIN {BUILD_SCHEMA}.source s ON s.sourcecode = r.sourcecode
    ON CONFLICT DO NOTHING
    """


def _region_name_sql() -> str:
    return f"""
    WITH annotated AS (
        SELECT r.*, max(priority) OVER (PARTITION BY regionid) AS toppriority
        FROM regionraw r
    ), names AS (
        SELECT regionid, sourcecode, name, namenorm AS normalized,
               CASE WHEN priority = toppriority THEN 'official' ELSE 'historical' END AS kind,
               'zh' AS language, priority
        FROM annotated WHERE namenorm <> ''
        UNION ALL
        SELECT regionid, sourcecode, shortname, shortnorm,
               CASE WHEN priority = toppriority THEN 'short' ELSE 'historical' END,
               'zh', priority + 5
        FROM annotated WHERE shortnorm <> '' AND shortnorm <> namenorm
        UNION ALL
        SELECT regionid, sourcecode, pinyin, pinyinnorm, 'pinyin', '', priority
        FROM annotated WHERE pinyinnorm <> ''
    ), chosen AS (
        SELECT DISTINCT ON (regionid, normalized, language) *
        FROM names
        ORDER BY regionid, normalized, language, priority DESC, sourcecode
    )
    INSERT INTO {BUILD_SCHEMA}.locationname(
        locationid, sourceid, name, normalizedname, nametype, languagecode, priority
    )
    SELECT n.regionid, s.sourceid, n.name, n.normalized, n.kind, n.language,
           least(32767, n.priority)
    FROM chosen n
    JOIN {BUILD_SCHEMA}.source s ON s.sourcecode = n.sourcecode
    ON CONFLICT DO NOTHING
    """


def _boundary_raw_table_sql() -> str:
    return """
    CREATE TEMP TABLE boundaryraw (
        adcode CHAR(12), sourcekey TEXT, centertext TEXT, polygontext TEXT, errormessage TEXT
    ) ON COMMIT PRESERVE ROWS
    """


def _boundary_copy_sql() -> str:
    return "COPY boundaryraw(adcode, sourcekey, centertext, polygontext, errormessage) FROM STDIN"


def _boundary_insert_sql() -> str:
    return f"""
    WITH chosen AS (
        SELECT DISTINCT ON (adcode) *
        FROM boundaryraw
        WHERE errormessage IS NULL
        ORDER BY adcode,
                 (polygontext IS NOT NULL) DESC,
                 length(polygontext) DESC NULLS LAST,
                 length(sourcekey), sourcekey
    )
    INSERT INTO {BUILD_SCHEMA}.boundary(
        regionid, sourceid, centergeom, boundarygeom, originalsystem, conversionmethod
    )
    SELECT r.regionid, %s,
           CASE WHEN b.centertext IS NULL THEN NULL ELSE ST_GeomFromEWKT(b.centertext) END,
           CASE WHEN b.polygontext IS NULL THEN NULL ELSE
               ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_GeomFromText(b.polygontext, 4326)), 3))
           END,
           'GCJ-02', 'iterative-forward-correction'
    FROM chosen b
    JOIN {BUILD_SCHEMA}.region r ON r.adcode = b.adcode
    ON CONFLICT (regionid) DO UPDATE SET
        centergeom = excluded.centergeom,
        boundarygeom = excluded.boundarygeom,
        sourceid = excluded.sourceid
    """


def _geo_raw_table_sql() -> str:
    return """
    CREATE TEMP TABLE georaw (
        locationid UUID, geonameid BIGINT, canonicalname TEXT, pointtext TEXT,
        importance DOUBLE PRECISION, asciiname TEXT, featureclass CHAR(1), featurecode TEXT,
        countrycode CHAR(2), admin1 TEXT, admin2 TEXT, admin3 TEXT, admin4 TEXT,
        population BIGINT, elevation INTEGER, dem INTEGER, timezone TEXT,
        modifiedon DATE, namesjson JSONB
    ) ON COMMIT PRESERVE ROWS
    """


def _geo_copy_sql() -> str:
    return """
    COPY georaw(
        locationid, geonameid, canonicalname, pointtext, importance, asciiname,
        featureclass, featurecode, countrycode, admin1, admin2, admin3, admin4,
        population, elevation, dem, timezone, modifiedon, namesjson
    ) FROM STDIN
    """


def _geo_location_sql() -> str:
    return f"""
    INSERT INTO {BUILD_SCHEMA}.location(locationid, kind, canonicalname, pointgeom, importance)
    SELECT locationid, 'place', canonicalname, ST_GeomFromEWKT(pointtext), importance
    FROM georaw
    """


def _geo_source_sql() -> str:
    return f"""
    INSERT INTO {BUILD_SCHEMA}.locationsource(locationid, sourceid, sourcekey, rawname, isprimary)
    SELECT locationid, %s, geonameid::text, canonicalname, true FROM georaw
    """


def _geoplace_sql() -> str:
    return f"""
    INSERT INTO {BUILD_SCHEMA}.geoplace(
        locationid, geonameid, asciiname, featureclass, featurecode, countrycode,
        admin1, admin2, admin3, admin4, population, elevation, dem, timezone, modifiedon
    )
    SELECT locationid, geonameid, asciiname, featureclass, featurecode, countrycode,
           admin1, admin2, admin3, admin4, population, elevation, dem, timezone, modifiedon
    FROM georaw
    """


def _geo_name_sql() -> str:
    return f"""
    INSERT INTO {BUILD_SCHEMA}.locationname(
        locationid, sourceid, name, normalizedname, nametype, languagecode, priority
    )
    SELECT g.locationid, %s, n.name, n.normalized, n.kind, n.language, n.priority
    FROM georaw g
    CROSS JOIN LATERAL jsonb_to_recordset(g.namesjson) AS n(
        name TEXT, normalized TEXT, kind TEXT, language TEXT, priority SMALLINT
    )
    WHERE n.normalized <> ''
    ON CONFLICT DO NOTHING
    """


def _poi_raw_table_sql() -> str:
    return """
    CREATE TEMP TABLE poiraw (
        locationid UUID, elementtype TEXT, elementid BIGINT, canonicalname TEXT,
        category TEXT, typename TEXT, address TEXT, imageurl TEXT,
        sourceadcode CHAR(12), sourceurl TEXT, pointtext TEXT, areawkb TEXT,
        namesjson JSONB
    ) ON COMMIT PRESERVE ROWS
    """


def _poi_copy_sql() -> str:
    return """
    COPY poiraw(
        locationid, elementtype, elementid, canonicalname, category, typename,
        address, imageurl, sourceadcode, sourceurl, pointtext, areawkb, namesjson
    ) FROM STDIN
    """


def _poi_location_sql() -> str:
    return f"""
    INSERT INTO {BUILD_SCHEMA}.location(locationid, kind, canonicalname, pointgeom, importance)
    SELECT locationid, 'poi', canonicalname,
           CASE
               WHEN pointtext IS NOT NULL THEN ST_GeomFromEWKT(pointtext)
               ELSE ST_PointOnSurface(
                   ST_MakeValid(
                       ST_GeomFromWKB(
                           decode(regexp_replace(areawkb, '^0x', ''), 'hex'),
                           4326
                       )
                   )
               )
           END,
           1
    FROM poiraw
    """


def _poi_source_sql() -> str:
    return f"""
    INSERT INTO {BUILD_SCHEMA}.locationsource(locationid, sourceid, sourcekey, rawname, isprimary)
    SELECT locationid, %s, elementtype || ':' || elementid, canonicalname, true
    FROM poiraw
    """


def _poi_insert_sql() -> str:
    return f"""
    INSERT INTO {BUILD_SCHEMA}.poi(
        locationid, elementtype, elementid, category, typename, address,
        imageurl, sourceadcode, sourceurl
    )
    SELECT locationid, elementtype, elementid, category, typename, address,
           imageurl, sourceadcode, sourceurl
    FROM poiraw
    """


def _poi_name_sql() -> str:
    return f"""
    INSERT INTO {BUILD_SCHEMA}.locationname(
        locationid, sourceid, name, normalizedname, nametype, languagecode, priority
    )
    SELECT p.locationid, %s, n.name, n.normalized, n.kind, n.language, n.priority
    FROM poiraw p
    CROSS JOIN LATERAL jsonb_to_recordset(p.namesjson) AS n(
        name TEXT, normalized TEXT, kind TEXT, language TEXT, priority SMALLINT
    )
    WHERE n.normalized <> ''
    ON CONFLICT DO NOTHING
    """


def _source_code_match_sql() -> str:
    return f"""
    INSERT INTO {BUILD_SCHEMA}.regionmatch(locationid, regionid, matchmethod, confidence)
    SELECT p.locationid, r.regionid, 'sourcecode', 1.000
    FROM {BUILD_SCHEMA}.poi p
    JOIN {BUILD_SCHEMA}.region r ON r.adcode = p.sourceadcode
    WHERE p.sourceadcode IS NOT NULL
    ON CONFLICT (locationid) DO NOTHING
    """


def _spatial_match_sql() -> str:
    return f"""
    INSERT INTO {BUILD_SCHEMA}.regionmatch(locationid, regionid, matchmethod, confidence)
    SELECT l.locationid, matched.regionid, 'spatial', 0.900
    FROM {BUILD_SCHEMA}.location l
    JOIN LATERAL (
        SELECT r.regionid
        FROM {BUILD_SCHEMA}.boundary b
        JOIN {BUILD_SCHEMA}.region r ON r.regionid = b.regionid
        WHERE b.boundarygeom IS NOT NULL AND ST_Covers(b.boundarygeom, l.pointgeom)
        ORDER BY r.level DESC
        LIMIT 1
    ) matched ON true
    LEFT JOIN {BUILD_SCHEMA}.regionmatch existing ON existing.locationid = l.locationid
    WHERE l.kind IN ('place', 'poi') AND l.pointgeom IS NOT NULL
      AND existing.locationid IS NULL
    ON CONFLICT (locationid) DO NOTHING
    """


def _unmatched_sql() -> str:
    return f"""
    INSERT INTO {BUILD_SCHEMA}.regionmatch(locationid, regionid, matchmethod, confidence)
    SELECT l.locationid, NULL, 'unmatched', 0
    FROM {BUILD_SCHEMA}.location l
    LEFT JOIN {BUILD_SCHEMA}.regionmatch m ON m.locationid = l.locationid
    WHERE l.kind IN ('place', 'poi') AND m.locationid IS NULL
    """


if __name__ == "__main__":
    main()
