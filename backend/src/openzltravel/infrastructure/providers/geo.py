"""地图 Provider 共用的纯计算与不可信响应解析。"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from openzltravel.domain.models import Poi, RouteSegment

from .base import ProviderError


def mapping(value: Any) -> dict[str, Any]:
    """仅接受 JSON 对象，其他上游值收敛为空对象。"""

    return value if isinstance(value, dict) else {}


def list_values(value: Any) -> list[Any]:
    """仅接受 JSON 数组。"""

    return value if isinstance(value, list) else []


def dict_items(value: Any) -> list[dict[str, Any]]:
    """返回 JSON 数组中的对象项，跳过脏数据。"""

    return [item for item in list_values(value) if isinstance(item, dict)]


def first_mapping(value: Any) -> dict[str, Any] | None:
    """返回数组中的第一个对象。"""

    return next(iter(dict_items(value)), None)


def text(value: Any) -> str:
    """将缺失文本统一为空字符串。"""

    if isinstance(value, list):
        return "".join(str(item) for item in value)
    return str(value or "").strip()


def http_url(value: Any) -> str | None:
    """仅保留合法 HTTP(S) URL，不下载或代理第三方图片。"""

    if isinstance(value, list):
        return next((url for item in value if (url := http_url(item))), None)
    if isinstance(value, dict):
        return http_url(value.get("url") or value.get("imageUrl"))
    if isinstance(value, str):
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return value
    return None


def parse_location(value: Any, *, from_amap: bool = False) -> tuple[float, float] | None:
    """解析高德的 ``经度,纬度``，需要时转为领域 WGS-84 坐标。"""

    if not isinstance(value, str) or "," not in value:
        return None
    longitude_text, latitude_text = value.split(",", 1)
    try:
        latitude, longitude = float(latitude_text), float(longitude_text)
    except ValueError:
        return None
    return gcj02_to_wgs84(latitude, longitude) if from_amap else (latitude, longitude)


def parse_polyline(value: Any) -> list[tuple[float, float]]:
    """解析真实高德轨迹点，不用 POI 坐标伪造道路线。"""

    if not isinstance(value, str):
        return []
    return [point for item in value.split(";") if (point := parse_location(item, from_amap=True))]


def amap_location(poi: Poi) -> str:
    """在高德 HTTP 边界把 WGS-84 转换为 GCJ-02。"""

    latitude, longitude = wgs84_to_gcj02(poi.latitude, poi.longitude)
    return f"{longitude},{latitude}"


def route_metrics(route: dict[str, Any]) -> tuple[float, int]:
    """从真实路线响应读取距离和时长，缺失时拒绝伪造零值。"""

    distance = nonnegative_number(route.get("distance"))
    duration = nonnegative_number(route.get("duration"))
    if distance is None or duration is None:
        raise ProviderError("route_not_found", "路线服务返回的数据不完整")
    return round(distance / 1000, 2), max(1, round(duration / 60))


def nonnegative_number(value: Any) -> float | None:
    """将有限非负数转为浮点数。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def local_routes(day_pois: Sequence[Poi], mode: str) -> list[RouteSegment]:
    """为普通步行/驾车生成明确标记的本地估算。"""

    return [
        local_route(left, right, _auto_mode(left, right, mode))
        for left, right in zip(day_pois, day_pois[1:], strict=False)
    ]


def local_route(from_poi: Poi, to_poi: Poi, mode: str) -> RouteSegment:
    """计算两个真实 POI 之间的球面距离估算，不生成 polyline。"""

    distance = _haversine(from_poi, to_poi)
    distance *= 1.2 if mode == "walk" else 1.4
    speed = 4.5 if mode == "walk" else 25.0
    return RouteSegment(
        from_poi_id=from_poi.id,
        to_poi_id=to_poi.id,
        distance_km=round(distance, 2),
        duration_minutes=max(1, round(distance / speed * 60)),
        mode="步行估算" if mode == "walk" else "驾车估算",
        polyline=[],
        source="local_estimate",
    )


def gcj02_to_wgs84(latitude: float, longitude: float) -> tuple[float, float]:
    """将高德 GCJ-02 坐标近似还原为 WGS-84。"""

    if _outside_china(latitude, longitude):
        return latitude, longitude
    adjusted_lat, adjusted_lon = wgs84_to_gcj02(latitude, longitude)
    return latitude * 2 - adjusted_lat, longitude * 2 - adjusted_lon


def wgs84_to_gcj02(latitude: float, longitude: float) -> tuple[float, float]:
    """将领域 WGS-84 坐标转为高德 GCJ-02。"""

    if _outside_china(latitude, longitude):
        return latitude, longitude
    d_lat = _transform_lat(longitude - 105.0, latitude - 35.0)
    d_lon = _transform_lon(longitude - 105.0, latitude - 35.0)
    rad_lat = latitude / 180.0 * math.pi
    magic = 1 - 0.00669342162296594323 * math.sin(rad_lat) ** 2
    root = math.sqrt(magic)
    d_lat = d_lat * 180.0 / (6335552.717000426 / (magic * root) * math.pi)
    d_lon = d_lon * 180.0 / (6378245.0 / root * math.cos(rad_lat) * math.pi)
    return latitude + d_lat, longitude + d_lon


def _auto_mode(left: Poi, right: Poi, mode: str) -> str:
    if mode != "auto":
        return mode
    return "walk" if _haversine(left, right) <= 3 else "driving"


def _haversine(left: Poi, right: Poi) -> float:
    radius = 6371.0
    lat1, lat2 = math.radians(left.latitude), math.radians(right.latitude)
    delta_lat = math.radians(right.latitude - left.latitude)
    delta_lon = math.radians(right.longitude - left.longitude)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return radius * 2 * math.asin(math.sqrt(value))


def _outside_china(latitude: float, longitude: float) -> bool:
    return not (73.0 <= longitude <= 135.0 and 3.0 <= latitude <= 54.0)


def _transform_lat(x: float, y: float) -> float:
    value = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
    value += 0.2 * math.sqrt(abs(x))
    value += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    value += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
    return value + (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3


def _transform_lon(x: float, y: float) -> float:
    value = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
    value += 0.1 * math.sqrt(abs(x))
    value += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    value += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
    return value + (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x * math.pi / 30)) * 2 / 3
