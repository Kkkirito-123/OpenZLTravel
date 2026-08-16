"""地图 Provider 共用的坐标、响应解析和本地路线估算。

领域层统一使用 WGS-84。高德调用和响应转换只在边界处理 GCJ-02，避免本地 OSM 数据与
高德地图坐标混用造成偏移。本文件不发起网络请求，也不依赖任何具体地图供应商。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from app.errors import ProviderError
from app.models import Coordinate, DataSource, Poi, RouteSegment


def mapping(value: Any) -> dict[str, Any]:
    """把不可信的上游字段收敛为字典，解析层不依赖响应结构完整。"""

    return value if isinstance(value, dict) else {}


def list_values(value: Any) -> list[Any]:
    """仅接受 JSON 数组，避免字符串或字典被误当成可迭代业务数据。"""

    return value if isinstance(value, list) else []


def dict_items(value: Any) -> list[dict[str, Any]]:
    """提取数组中的对象项，跳过供应商混入的空值或非对象项。"""

    return [item for item in list_values(value) if isinstance(item, dict)]


def first_mapping(value: Any) -> dict[str, Any] | None:
    """返回数组中第一条对象记录，不把异常项当作事实。"""

    return next(iter(dict_items(value)), None)


def text(value: Any) -> str:
    """以空字符串表示缺失文本，供供应商解析边界复用。"""

    if isinstance(value, list):
        return "".join(str(item) for item in value)
    return str(value or "")


def photo_url(value: Any) -> str | None:
    """只接受供应商照片列表中的 HTTP(S) 地址，不下载第三方图片。"""

    if not isinstance(value, list):
        return None
    for photo in value:
        url = photo.get("url") if isinstance(photo, dict) else None
        parsed = urlsplit(url) if isinstance(url, str) else None
        if parsed and parsed.scheme in {"http", "https"} and parsed.netloc:
            return url
    return None


def parse_location(value: Any, *, from_amap: bool = False) -> tuple[float | None, float | None]:
    """解析 ``经度,纬度`` 字符串，并在需要时转换为领域坐标。"""

    if not isinstance(value, str) or "," not in value:
        return None, None
    longitude_text, latitude_text = value.split(",", 1)
    try:
        latitude, longitude = float(latitude_text), float(longitude_text)
    except ValueError:
        return None, None
    return gcj02_to_wgs84(latitude, longitude) if from_amap else (latitude, longitude)


def amap_location(poi: Poi) -> str:
    """调用高德前把领域层 WGS-84 坐标转换为高德使用的 GCJ-02。"""

    latitude, longitude = wgs84_to_gcj02(poi.latitude, poi.longitude)
    return f"{longitude},{latitude}"


def parse_polyline(value: str) -> list[Coordinate]:
    """解析高德 GCJ-02 轨迹点；异常点被跳过，不拼接虚假直线。"""

    points: list[Coordinate] = []
    for item in value.split(";"):
        latitude, longitude = parse_location(item, from_amap=True)
        if latitude is not None and longitude is not None:
            points.append(Coordinate(latitude=latitude, longitude=longitude))
    return points


def route_metrics(route: dict[str, Any]) -> tuple[float, int]:
    """校验路线核心指标，缺失时让调用方走稳定降级而不是伪造零距离。"""

    distance = nonnegative_number(route.get("distance"))
    duration = nonnegative_number(route.get("duration"))
    if distance is None or duration is None:
        raise ProviderError("route_not_found", "路线服务返回的数据不完整")
    return round(distance / 1000, 2), max(1, round(duration / 60))


def nonnegative_number(value: Any) -> float | None:
    """只接受有限且非负的外部数值。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def local_routes(day_pois: Sequence[Poi], mode: str) -> list[RouteSegment]:
    """为普通步行/驾车提供低成本估算，明确不生成虚假轨迹。"""

    return [
        local_route(left, right, _choose_auto_mode(left, right, mode))
        for left, right in zip(day_pois, day_pois[1:], strict=False)
    ]


def local_route(
    from_poi: Poi,
    to_poi: Poi,
    mode: str,
    via_pois: Sequence[Poi] = (),
) -> RouteSegment:
    """计算一段本地道路系数估算，用于实时服务不可用时的透明降级。"""

    points = [from_poi, *via_pois, to_poi]
    distance = sum(_haversine(left, right) for left, right in zip(points, points[1:], strict=False))
    distance *= 1.2 if mode == "walk" else 1.4
    speed = 4.5 if mode == "walk" else 25.0
    label = "步行估算" if mode == "walk" else "驾车估算"
    duration = max(1, round(distance / speed * 60))
    return RouteSegment(
        from_poi_id=from_poi.id,
        to_poi_id=to_poi.id,
        via_poi_ids=[poi.id for poi in via_pois],
        distance_km=round(distance, 2),
        duration_minutes=duration,
        mode=label,
        source=DataSource(provider="local_estimate", freshness="estimated"),
    )


def now() -> datetime:
    """统一生成带时区的供应商抓取时间。"""

    return datetime.now(timezone.utc)


def gcj02_to_wgs84(latitude: float, longitude: float) -> tuple[float, float]:
    """将高德返回的 GCJ-02 坐标近似还原到领域层统一使用的 WGS-84。"""

    if _outside_china(latitude, longitude):
        return latitude, longitude
    adjusted_lat, adjusted_lon = wgs84_to_gcj02(latitude, longitude)
    return latitude * 2 - adjusted_lat, longitude * 2 - adjusted_lon


def wgs84_to_gcj02(latitude: float, longitude: float) -> tuple[float, float]:
    """把领域坐标转换为高德 JavaScript 与 Web Service 使用的 GCJ-02。"""

    if _outside_china(latitude, longitude):
        return latitude, longitude
    d_lat = _transform_lat(longitude - 105.0, latitude - 35.0)
    d_lon = _transform_lon(longitude - 105.0, latitude - 35.0)
    rad_lat = latitude / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - 0.00669342162296594323 * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = d_lat * 180.0 / ((6335552.717000426 * (magic * sqrt_magic)) * math.pi)
    d_lon = d_lon * 180.0 / (6378245.0 / sqrt_magic * math.cos(rad_lat) * math.pi)
    return latitude + d_lat, longitude + d_lon


def _choose_auto_mode(from_poi: Poi, to_poi: Poi, mode: str) -> str:
    if mode != "auto":
        return mode
    return "walk" if _haversine(from_poi, to_poi) <= 3 else "driving"


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
    value = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y
    value += 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    value += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    value += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    value += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return value


def _transform_lon(x: float, y: float) -> float:
    value = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
    value += 0.1 * math.sqrt(abs(x))
    value += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    value += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    value += (150.0 * math.sin(x / 12.0 * math.pi) + 300 * math.sin(x * math.pi / 30.0)) * 2.0 / 3.0
    return value
