"""地点目录共用的名称、稳定编号和坐标处理。

数据库统一保存 WGS-84。AreaCity 的 GCJ-02 只在离线导入边界转换，避免运行时
把两套坐标混在一起。这里不访问数据库，也不发起网络请求。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import urlsplit
from uuid import UUID, uuid5

CATALOG_NAMESPACE = UUID("59edc06a-4a99-5d64-a3aa-d7cc7a93b756")
CHINESE_TEXT = re.compile(r"[\u3400-\u9fff]")
ADMIN_SUFFIXES = (
    "特别行政区",
    "维吾尔自治区",
    "壮族自治区",
    "回族自治区",
    "自治区",
    "自治州",
    "自治县",
    "街道办事处",
    "社区居委会",
    "村民委员会",
    "居民委员会",
    "街道",
    "地区",
    "村委会",
    "社区",
    "省",
    "市",
    "区",
    "县",
    "盟",
    "旗",
    "镇",
    "乡",
    "村",
)


def stable_uuid(key: str) -> UUID:
    """根据稳定来源键生成可跨数据库重建复用的 UUID。"""

    return uuid5(CATALOG_NAMESPACE, key)


def region_uuid(adcode: str) -> UUID:
    """根据十二位行政代码生成行政区 UUID。"""

    return stable_uuid(f"region:{normalize_adcode(adcode)}")


def geoname_uuid(geonameid: str | int) -> UUID:
    """根据 GeoNames 官方编号生成地点 UUID。"""

    return stable_uuid(f"geonames:{geonameid}")


def osm_uuid(elementtype: str, elementid: str | int) -> UUID:
    """根据 OSM 元素类型和编号生成 POI UUID。"""

    return stable_uuid(f"osm:{elementtype}:{elementid}")


def normalize_adcode(value: str | int) -> str:
    """将二至十二位行政代码统一右侧补零为十二位。"""

    text = str(value).strip()
    if not text.isdigit() or not 1 <= len(text) <= 12:
        raise ValueError(f"非法行政代码：{value}")
    return text.ljust(12, "0")


def adcode_level(value: str | int) -> int:
    """根据来源代码长度确定层级，用于识别直管县等占位节点。"""

    sourcecode = str(value).strip()
    normalize_adcode(sourcecode)
    if set(sourcecode) == {"0"}:
        return 0
    for maxlength, level in ((2, 1), (4, 2), (6, 3), (9, 4)):
        if len(sourcecode) <= maxlength:
            return level
    return 5


def normalize_name(value: str) -> str:
    """生成精确和模糊检索共用的 Unicode 规范名称。"""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def short_region_name(value: str) -> str:
    """移除一个常见行政后缀，保留可回溯的官方名称。"""

    name = value.strip()
    suffix = next((item for item in ADMIN_SUFFIXES if name.endswith(item)), "")
    short = name[: -len(suffix)] if suffix else name
    return short or name


def preferred_geoname(name: str, aliases: Iterable[str]) -> str:
    """优先选择中文别名作为 GeoNames 展示名称。"""

    chinese = [item for item in aliases if CHINESE_TEXT.search(item)]
    city_name = next((item for item in chinese if item.endswith("市")), None)
    return city_name or next(iter(chinese), name)


def valid_http_url(value: str) -> str | None:
    """只保留合法 HTTP(S) 地址，第三方图片不下载也不代理。"""

    parsed = urlsplit(value.strip())
    return value.strip() if parsed.scheme in {"http", "https"} and parsed.netloc else None


def point_ewkt(latitude: float, longitude: float) -> str:
    """返回 PostgreSQL COPY 可直接接收的 WGS-84 点。"""

    _validate_coordinate(latitude, longitude)
    return f"SRID=4326;POINT({longitude:.8f} {latitude:.8f})"


def parse_gcj02_point(value: str) -> str | None:
    """解析 AreaCity 中的 GCJ-02 中心点并转换为 WGS-84 EWKT。"""

    if not value or value == "EMPTY":
        return None
    parts = value.strip().split()
    if len(parts) != 2:
        raise ValueError(f"非法中心点：{value[:80]}")
    longitude, latitude = float(parts[0]), float(parts[1])
    wgs_latitude, wgs_longitude = gcj02_to_wgs84(latitude, longitude)
    return point_ewkt(wgs_latitude, wgs_longitude)


def parse_gcj02_multipolygon(value: str) -> str | None:
    """把 AreaCity 多地块与孔洞格式转换为 WGS-84 MultiPolygon WKT。"""

    if not value or value == "EMPTY":
        return None
    polygons = [_polygon_wkt(item) for item in value.split(";") if item.strip()]
    valid = [item for item in polygons if item]
    if not valid:
        return None
    return f"MULTIPOLYGON({','.join(valid)})"


def gcj02_to_wgs84(latitude: float, longitude: float) -> tuple[float, float]:
    """迭代还原 GCJ-02 坐标，直到前向转换误差小于约一厘米。"""

    if _outside_china(latitude, longitude):
        return latitude, longitude
    result_latitude, result_longitude = latitude, longitude
    for _ in range(10):
        converted_latitude, converted_longitude = wgs84_to_gcj02(result_latitude, result_longitude)
        latitude_error = converted_latitude - latitude
        longitude_error = converted_longitude - longitude
        result_latitude -= latitude_error
        result_longitude -= longitude_error
        if max(abs(latitude_error), abs(longitude_error)) < 1e-7:
            break
    return result_latitude, result_longitude


def wgs84_to_gcj02(latitude: float, longitude: float) -> tuple[float, float]:
    """将 WGS-84 转为高德使用的 GCJ-02，供反向迭代求解。"""

    if _outside_china(latitude, longitude):
        return latitude, longitude
    latitude_delta = _transform_lat(longitude - 105.0, latitude - 35.0)
    longitude_delta = _transform_lon(longitude - 105.0, latitude - 35.0)
    radians = latitude / 180.0 * math.pi
    magic = 1 - 0.00669342162296594323 * math.sin(radians) ** 2
    root = math.sqrt(magic)
    latitude_delta *= 180.0 / (6335552.717000426 * magic * root * math.pi)
    longitude_delta *= 180.0 / (6378245.0 / root * math.cos(radians) * math.pi)
    return latitude + latitude_delta, longitude + longitude_delta


def _polygon_wkt(value: str) -> str | None:
    rings = [_ring_wkt(item) for item in value.split("~") if item.strip()]
    valid = [item for item in rings if item]
    return f"({','.join(valid)})" if valid else None


def _ring_wkt(value: str) -> str | None:
    points: list[tuple[float, float]] = []
    for item in value.split(","):
        parts = item.strip().split()
        if len(parts) != 2:
            continue
        longitude, latitude = float(parts[0]), float(parts[1])
        _validate_coordinate(latitude, longitude)
        wgs_latitude, wgs_longitude = gcj02_to_wgs84(latitude, longitude)
        points.append((wgs_longitude, wgs_latitude))
    if len(set(points)) < 3:
        return None
    if points[0] != points[-1]:
        points.append(points[0])
    coordinates = ",".join(f"{longitude:.8f} {latitude:.8f}" for longitude, latitude in points)
    return f"({coordinates})"


def _validate_coordinate(latitude: float, longitude: float) -> None:
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError(f"坐标超出范围：{longitude},{latitude}")


def _outside_china(latitude: float, longitude: float) -> bool:
    return not (73.0 <= longitude <= 135.0 and 3.0 <= latitude <= 54.0)


def _transform_lat(x: float, y: float) -> float:
    value = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y
    value += 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    value += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    value += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
    value += (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    return value


def _transform_lon(x: float, y: float) -> float:
    value = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
    value += 0.1 * math.sqrt(abs(x))
    value += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    value += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
    value += (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
    return value
