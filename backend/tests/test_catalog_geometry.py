"""地点目录名称、稳定编号和坐标转换测试。"""

from __future__ import annotations

import pytest

from catalog_builder.geometry import (
    adcode_level,
    gcj02_to_wgs84,
    normalize_adcode,
    normalize_name,
    parse_gcj02_multipolygon,
    region_uuid,
    short_region_name,
    valid_http_url,
    wgs84_to_gcj02,
)


def test_adcode_normalization_keeps_region_uuid_stable() -> None:
    """短代码与十二位代码必须指向同一个行政节点。"""

    assert normalize_adcode("6101") == "610100000000"
    assert region_uuid("6101") == region_uuid("610100000000")


@pytest.mark.parametrize(
    ("sourcecode", "level"),
    [
        ("0", 0),
        ("61", 1),
        ("6101", 2),
        ("419001", 3),
        ("610116001", 4),
        ("610116001001", 5),
    ],
)
def test_source_adcode_length_maps_to_admin_level(sourcecode: str, level: int) -> None:
    """来源短代码的有效长度决定正式行政层级。"""

    assert adcode_level(sourcecode) == level


@pytest.mark.parametrize("value", ["", "陕西", "1234567890123"])
def test_invalid_adcode_is_rejected(value: str) -> None:
    """非法代码不能静默进入行政树。"""

    with pytest.raises(ValueError, match="非法行政代码"):
        normalize_adcode(value)


def test_search_names_are_normalized_without_merging_entities() -> None:
    """名称归一化只服务检索，不创建第二个行政节点。"""

    assert normalize_name("Xi An") == "xian"
    assert normalize_name("西安市") == "西安市"
    assert short_region_name("西安市") == "西安"


def test_gcj02_round_trip_is_close_to_original_wgs84() -> None:
    """边界坐标转换往返误差应保持在约一米以内。"""

    latitude, longitude = 39.9042, 116.4074
    gcj_latitude, gcj_longitude = wgs84_to_gcj02(latitude, longitude)
    restored = gcj02_to_wgs84(gcj_latitude, gcj_longitude)

    assert restored == pytest.approx((latitude, longitude), abs=1e-6)


def test_multipolygon_closes_outer_rings_and_keeps_holes() -> None:
    """多地块和孔洞都应保留，未闭合环由导入器确定性闭合。"""

    polygon = "0 0,1 0,1 1~0.2 0.2,0.8 0.2,0.8 0.8;2 2,3 2,3 3"

    assert parse_gcj02_multipolygon(polygon) == (
        "MULTIPOLYGON(((0.00000000 0.00000000,1.00000000 0.00000000,"
        "1.00000000 1.00000000,0.00000000 0.00000000),"
        "(0.20000000 0.20000000,0.80000000 0.20000000,"
        "0.80000000 0.80000000,0.20000000 0.20000000)),"
        "((2.00000000 2.00000000,3.00000000 2.00000000,"
        "3.00000000 3.00000000,2.00000000 2.00000000)))"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com/a.jpg", "https://example.com/a.jpg"),
        ("http://example.com/a.jpg", "http://example.com/a.jpg"),
        ("file:///tmp/a.jpg", None),
        ("javascript:alert(1)", None),
    ],
)
def test_only_http_image_urls_are_kept(value: str, expected: str | None) -> None:
    """第三方图片只保存可公开访问的 HTTP(S) URL。"""

    assert valid_http_url(value) == expected
