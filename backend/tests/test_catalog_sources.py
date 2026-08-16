"""公共地点库来源解析测试。"""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path
from typing import Any

import pytest

from catalog_builder.geometry import region_uuid
from catalog_builder.sources import (
    ROOT_UUID,
    _node_poi,
    iter_areacity_regions,
    iter_boundaries,
    iter_geonames,
    iter_osm_pois,
)


class FakeLocation:
    """模拟 pyosmium 的有效节点坐标。"""

    lat = 34.2595
    lon = 108.9470

    def valid(self) -> bool:
        """测试坐标始终有效。"""

        return True


class FakeNode:
    """提供 OSM 节点解析所需的最小接口。"""

    def __init__(self, tags: dict[str, str]) -> None:
        self.id = 123
        self.tags = tags
        self.location = FakeLocation()


def test_areacity_builds_the_expected_tree_and_aliases(tmp_path: Path) -> None:
    """AreaCity 内部父代码应归一到稳定的十二位行政代码。"""

    source = tmp_path / "area.csv"
    rows: list[dict[str, Any]] = [
        {
            "id": "61",
            "pid": "0",
            "deep": "0",
            "name": "陕西",
            "pinyin_prefix": "s",
            "pinyin": "shan xi",
            "ext_id": "610000000000",
            "ext_name": "陕西省",
        },
        {
            "id": "6101",
            "pid": "61",
            "deep": "1",
            "name": "西安",
            "pinyin_prefix": "x",
            "pinyin": "xi an",
            "ext_id": "610100000000",
            "ext_name": "西安市",
        },
    ]
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    province, city = iter_areacity_regions(source)

    assert province.parentid == ROOT_UUID
    assert city.parentid == region_uuid("610000000000")
    assert city.path == "cn.r61.r6101"
    assert city.name == "西安市"
    assert city.shortname == "西安"
    assert city.pinyin == "xi an"


def test_geonames_keeps_all_names_and_coordinates(tmp_path: Path) -> None:
    """GeoNames 主名称、ASCII 名和中文别名必须落到同一地点。"""

    fields = [
        "1790630",
        "Xi'an",
        "Xi'an",
        "Chang'an,xi an,西安市",
        "34.25833",
        "108.92861",
        "P",
        "PPLA",
        "CN",
        "",
        "26",
        "6101",
        "",
        "",
        "6501190",
        "",
        "405",
        "Asia/Shanghai",
        "2025-01-01",
    ]
    archive = tmp_path / "CN.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("CN.txt", "\t".join(fields) + "\n")

    [record] = list(iter_geonames(archive))

    assert record.canonicalname == "西安市"
    assert record.point == "SRID=4326;POINT(108.92861000 34.25833000)"
    assert {name.name for name in record.names} >= {"Xi'an", "西安市", "Chang'an"}
    assert len({name.normalized for name in record.names}) == len(record.names)


def test_osm_only_accepts_named_travel_pois() -> None:
    """道路和普通建筑不能进入 POI，命名旅行地点可以进入。"""

    assert _node_poi(FakeNode({"highway": "primary", "name": "测试道路"})) is None
    assert _node_poi(FakeNode({"building": "yes", "name": "普通建筑"})) is None

    poi = _node_poi(
        FakeNode(
            {
                "tourism": "museum",
                "name": "陕西历史博物馆",
                "ref:GB:adcode": "610113",
                "image": "https://example.com/museum.jpg",
            }
        )
    )

    assert poi is not None
    assert poi.category == "attraction"
    assert poi.sourceadcode == "610113000000"
    assert poi.imageurl == "https://example.com/museum.jpg"


def test_invalid_boundary_is_reported_without_stopping_the_stream(tmp_path: Path) -> None:
    """单条无法形成面的边界应产生错误记录，后续记录仍可继续导入。"""

    source = tmp_path / "boundaries.csv"
    _write_rows(
        source,
        ["id", "pid", "deep", "name", "ext_path", "geo", "polygon"],
        [
            ["610100", "61", "2", "错误边界", "陕西 西安", "108.9 34.1", "1 1,1 1"],
            ["610116", "6101", "2", "长安区", "陕西 西安 长安", "108.9 34.1", "EMPTY"],
            ["710101", "71", "2", "中正区", "台湾 台北 中正", "EMPTY", "EMPTY"],
        ],
    )

    invalid, valid, missing = iter_boundaries(source)

    assert invalid.error == "边界文本不包含有效多边形"
    assert valid.error is None
    assert valid.center is not None
    assert missing.error is None
    assert missing.center is None
    assert missing.polygon is None


def test_pyosmium_reads_nodes_and_closed_areas(tmp_path: Path) -> None:
    """真实 pyosmium 流程必须同时提取节点与闭合面，且节点索引落盘。"""

    pytest.importorskip("osmium")
    source = tmp_path / "sample.osm"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="OpenZLTravel tests">
  <node id="1" lat="34.2500" lon="108.9400">
    <tag k="tourism" v="museum"/><tag k="name" v="节点博物馆"/>
  </node>
  <node id="2" lat="34.2510" lon="108.9410"/>
  <node id="3" lat="34.2510" lon="108.9420"/>
  <node id="4" lat="34.2520" lon="108.9420"/>
  <node id="5" lat="34.2520" lon="108.9410"/>
  <way id="10">
    <nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="5"/><nd ref="2"/>
    <tag k="tourism" v="hotel"/><tag k="name" v="面状酒店"/>
  </way>
  <node id="6" lat="34.2530" lon="108.9430"/>
  <node id="7" lat="34.2530" lon="108.9440"/>
  <node id="8" lat="34.2540" lon="108.9440"/>
  <node id="9" lat="34.2540" lon="108.9430"/>
  <way id="20">
    <nd ref="6"/><nd ref="7"/><nd ref="8"/><nd ref="9"/><nd ref="6"/>
  </way>
  <relation id="30">
    <member type="way" ref="20" role="outer"/>
    <tag k="type" v="multipolygon"/><tag k="tourism" v="attraction"/>
    <tag k="name" v="关系景点"/>
  </relation>
</osm>
""",
        encoding="utf-8",
    )

    records = list(iter_osm_pois(source, tmp_path / "nodes.cache"))

    assert {(record.elementtype, record.elementid) for record in records} == {
        ("node", 1),
        ("way", 10),
        ("relation", 30),
    }
    areas = [record for record in records if record.elementtype != "node"]
    assert all(record.point is None and record.areawkb for record in areas)


def _write_rows(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)
