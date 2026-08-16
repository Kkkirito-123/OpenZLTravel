"""PostGIS 小数据完整构建、原子切换与失败回滚测试。"""

from __future__ import annotations

import csv
import os
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from catalog_builder import build as build_module
from catalog_builder.build import BuildPaths, CatalogBuilder
from catalog_builder.geometry import osm_uuid, point_ewkt
from catalog_builder.sources import NameRaw, PoiRaw

TEST_DATABASE_NAME = "openzltravelcatalogtest"


def test_complete_build_repeat_publish_and_failure_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实数据库中重复构建应保留上一代，失败构建不得替换正式库。"""

    database_url = os.getenv("CATALOG_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("未配置 CATALOG_TEST_DATABASE_URL")
    if urlsplit(database_url).path.lstrip("/") != TEST_DATABASE_NAME:
        pytest.fail(f"集成测试只允许使用数据库 {TEST_DATABASE_NAME}")

    psycopg = pytest.importorskip("psycopg")
    paths = _write_sources(tmp_path)
    monkeypatch.setattr(build_module, "iter_osm_pois", _fake_osm_pois)

    with psycopg.connect(database_url) as connection:
        first = CatalogBuilder(connection, paths, require_full=False).run()
        cityid = _scalar_text(
            connection,
            "SELECT regionid::text FROM catalog.region WHERE adcode = '610100000000'",
        )
        assert first["region"] == 9
        assert _xian_aliases(connection) >= {"西安市", "西安", "xian"}
        assert (
            _scalar_text(
                connection,
                """
            SELECT nametype FROM catalog.locationname
            WHERE normalizedname = '旧西安市'
            """,
            )
            == "historical"
        )
        assert _match_methods(connection) == {"sourcecode", "spatial"}
        assert (
            _scalar_int(
                connection,
                "SELECT count(*) FROM catalog.region WHERE status = 'undetermined'",
            )
            == 1
        )
        assert (
            _scalar_int(
                connection,
                """
                SELECT count(*) FROM catalog.location
                WHERE pointgeom IS NOT NULL AND ST_SRID(pointgeom) = 4326
                """,
            )
            == 2
        )
        assert _scalar_bool(
            connection,
            "SELECT has_schema_privilege('catalogreader', 'catalog', 'USAGE')",
        )
        assert _scalar_bool(
            connection,
            "SELECT has_table_privilege('catalogreader', 'catalog.region', 'SELECT')",
        )
        assert not _scalar_bool(
            connection,
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'catalogreader'",
        )
        assert (
            _scalar_int(
                connection,
                """
            SELECT count(*) FROM catalog.region
            WHERE adcode = '441900000000' AND level = 2 AND parentid <> regionid
            """,
            )
            == 1
        )
        assert (
            _scalar_int(
                connection,
                "SELECT count(*) FROM catalog.boundary WHERE regionid IN "
                "(SELECT regionid FROM catalog.region WHERE adcode = '441900000000')",
            )
            == 1
        )

        CatalogBuilder(connection, paths, require_full=False).run()
        assert _schema_exists(connection, "catalogprevious")
        assert cityid == _scalar_text(
            connection,
            "SELECT regionid::text FROM catalog.region WHERE adcode = '610100000000'",
        )

        stable_count = _scalar_int(connection, "SELECT count(*) FROM catalog.location")
        monkeypatch.setattr(
            CatalogBuilder,
            "_import_geonames",
            _raise_test_failure,
        )
        with pytest.raises(RuntimeError, match="测试构建失败"):
            CatalogBuilder(connection, paths, require_full=False).run()

        assert _scalar_int(connection, "SELECT count(*) FROM catalog.location") == stable_count
        assert (
            _scalar_text(
                connection,
                "SELECT status FROM catalogbuild.build ORDER BY startedat DESC LIMIT 1",
            )
            == "failed"
        )


def _write_sources(root: Path) -> BuildPaths:
    modood = root / "modood"
    modood.mkdir()
    _write_csv(modood / "provinces.csv", ["code", "name"], [["61", "陕西省"]])
    _write_csv(
        modood / "cities.csv",
        ["code", "name", "provinceCode"],
        [["6101", "旧西安市", "61"]],
    )
    _write_csv(
        modood / "areas.csv",
        ["code", "name", "provinceCode", "cityCode"],
        [["610116", "长安区", "61", "6101"]],
    )
    _write_csv(
        modood / "streets.csv",
        ["code", "name", "areaCode", "provinceCode", "cityCode"],
        [["610116001", "韦曲街道", "610116", "61", "6101"]],
    )
    _write_csv(
        modood / "villages.csv",
        ["code", "name", "streetCode", "areaCode", "provinceCode", "cityCode"],
        [["610116001001", "测试村", "610116001", "610116", "61", "6101"]],
    )

    area = root / "area.csv"
    _write_csv(
        area,
        ["id", "pid", "deep", "name", "pinyin_prefix", "pinyin", "ext_id", "ext_name"],
        [
            ["61", "0", "0", "陕西", "s", "shan xi", "610000000000", "陕西省"],
            ["6101", "61", "1", "西安", "x", "xi an", "610100000000", "西安市"],
            ["610116", "6101", "2", "长安", "c", "chang an", "610116000000", "长安区"],
            [
                "610116001",
                "610116",
                "3",
                "韦曲",
                "w",
                "wei qu",
                "610116001000",
                "韦曲街道",
            ],
            ["44", "0", "0", "广东", "g", "guang dong", "440000000000", "广东省"],
            ["4419", "44", "1", "东莞", "d", "dong guan", "441900000000", "东莞市"],
            [
                "441900",
                "4419",
                "2",
                "东莞",
                "d",
                "dong guan",
                "441900000000",
                "东莞市",
            ],
            [
                "441900001",
                "441900",
                "3",
                "莞城",
                "g",
                "guan cheng",
                "441900001000",
                "莞城街道",
            ],
        ],
    )
    boundary = root / "boundary.csv"
    _write_csv(
        boundary,
        ["id", "pid", "deep", "name", "ext_path", "geo", "polygon"],
        [
            [
                "610116",
                "6101",
                "2",
                "长安区",
                "陕西省 西安市 长安区",
                "108.9 34.1",
                "107 33,110 33,110 35,107 35,107 33",
            ],
            [
                "4419",
                "44",
                "1",
                "东莞市",
                "广东省 东莞市",
                "113.75 23.02",
                "113 22,115 22,115 24,113 24,113 22",
            ],
            [
                "441900",
                "4419",
                "2",
                "东莞市",
                "广东省 东莞市 东莞市",
                "113.75 23.02",
                "113 22,115 22,115 24,113 24,113 22",
            ],
        ],
    )

    geonames = root / "CN.zip"
    geoname_fields = [
        "999001",
        "Test Place",
        "Test Place",
        "测试地名",
        "34.1",
        "108.9",
        "P",
        "PPL",
        "CN",
        "",
        "26",
        "6101",
        "",
        "",
        "100",
        "",
        "400",
        "Asia/Shanghai",
        "2025-01-01",
    ]
    with zipfile.ZipFile(geonames, "w") as output:
        output.writestr("CN.txt", "\t".join(geoname_fields) + "\n")

    archive = root / "modood.tgz"
    archive.write_bytes(b"test archive")
    osm = root / "sample.osm.pbf"
    osm.write_bytes(b"test pbf")
    return BuildPaths(modood, archive, area, boundary, geonames, osm)


def _write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)


def _fake_osm_pois(_source: Path, _index: Path) -> list[PoiRaw]:
    return [
        PoiRaw(
            locationid=osm_uuid("node", 7001),
            elementtype="node",
            elementid=7001,
            canonicalname="测试博物馆",
            category="attraction",
            typename="museum",
            address="西安市长安区",
            imageurl=None,
            sourceadcode="610116000000",
            sourceurl="https://www.openstreetmap.org/node/7001",
            point=point_ewkt(34.1, 108.9),
            areawkb=None,
            names=(NameRaw("测试博物馆", "测试博物馆", "official", "zh", 80),),
        )
    ]


def _raise_test_failure(_builder: CatalogBuilder) -> None:
    raise RuntimeError("测试构建失败")


def _xian_aliases(connection: object) -> set[str]:
    rows = connection.execute(  # type: ignore[attr-defined]
        """
        SELECT n.normalizedname
        FROM catalog.region r
        JOIN catalog.locationname n ON n.locationid = r.regionid
        WHERE r.adcode = '610100000000'
        """
    ).fetchall()
    return {str(row[0]) for row in rows}


def _match_methods(connection: object) -> set[str]:
    rows = connection.execute(  # type: ignore[attr-defined]
        "SELECT matchmethod FROM catalog.regionmatch"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _schema_exists(connection: object, schema: str) -> bool:
    row = connection.execute(  # type: ignore[attr-defined]
        "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = %s)",
        (schema,),
    ).fetchone()
    return bool(row and row[0])


def _scalar_int(connection: object, query: str) -> int:
    row = connection.execute(query).fetchone()  # type: ignore[attr-defined]
    return int(row[0])


def _scalar_text(connection: object, query: str) -> str:
    row = connection.execute(query).fetchone()  # type: ignore[attr-defined]
    return str(row[0])


def _scalar_bool(connection: object, query: str) -> bool:
    row = connection.execute(query).fetchone()  # type: ignore[attr-defined]
    return bool(row[0])
