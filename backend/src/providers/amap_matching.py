from __future__ import annotations

import re
from typing import Any, Literal

from domain.models import City, Poi

from .base import ProviderError, stable_fact_id
from .geo import http_url, parse_location, text

PoiCategory = Literal["attraction", "restaurant", "hotel"]


def city_from_geocode(query: str, geocode: dict[str, Any]) -> City:
    """把高德地理编码结果转换为带坐标和行政编码的城市事实。"""

    location = parse_location(geocode.get("location"), from_amap=True)
    if location is None:
        raise ProviderError("city_not_found", "高德地点结果缺少有效坐标")
    latitude, longitude = location
    return City(
        name=text(geocode.get("city")) or text(geocode.get("district")) or query,
        adcode=text(geocode.get("adcode")) or None,
        latitude=latitude,
        longitude=longitude,
    )


def is_administrative_query(query: str, geocode: dict[str, Any]) -> bool:
    """判断查询是否命中行政区，避免把城市表达降格为普通 POI。"""

    if text(geocode.get("level")) in {"国家", "省", "市", "区县", "乡镇"}:
        return True
    normalized = _normalize_name(query)
    names = {
        _normalize_name(text(geocode.get(field)))
        for field in ("province", "city", "district", "township")
        if text(geocode.get(field))
    }
    return normalized in names


def poi_from_item(item: dict[str, Any], category: PoiCategory) -> Poi | None:
    """把合法高德条目转换为 POI 事实，缺关键字段时拒绝构造。"""

    location = parse_location(item.get("location"), from_amap=True)
    raw_id, name = text(item.get("id")), text(item.get("name"))
    if location is None or not raw_id or not name:
        return None
    latitude, longitude = location
    type_name = text(item.get("type"))
    return Poi(
        id=stable_fact_id("poi-amap", raw_id),
        name=name,
        address=text(item.get("address")),
        category=category,
        latitude=latitude,
        longitude=longitude,
        type_name=type_name,
        image_url=http_url(item.get("photos")),
        tags=[value for value in type_name.split(";") if value],
    )


def best_poi_match(target: Poi, candidates: list[Poi]) -> Poi | None:
    """按类别、规范名和近邻距离选择目录 POI 的展示补全来源。"""

    same_category = [item for item in candidates if item.category == target.category]
    if not same_category:
        return None
    target_name = _normalize_name(target.name)
    exact = [item for item in same_category if _normalize_name(item.name) == target_name]
    if exact:
        return exact[0]
    named = [
        item
        for item in same_category
        if target_name in _normalize_name(item.name)
        or _normalize_name(item.name) in target_name
    ]
    if named:
        return min(named, key=lambda item: _distance(target, item))
    nearest = min(same_category, key=lambda item: _distance(target, item))
    return nearest if _distance(target, nearest) <= 0.03 else None


def best_query_match(query: str, candidates: list[Poi]) -> Poi | None:
    """按规范名称选择查询对应的高德 POI，不使用无依据的地理猜测。"""

    normalized = _normalize_name(query)
    exact = [item for item in candidates if _normalize_name(item.name) == normalized]
    if exact:
        return exact[0]
    return next(
        (
            item
            for item in candidates
            if normalized in _normalize_name(item.name)
            or _normalize_name(item.name) in normalized
        ),
        None,
    )


def _normalize_name(value: str) -> str:
    normalized = re.sub(r"[市区县酒店宾馆旅馆度假村]+$", "", value.strip())
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", normalized).casefold()


def _distance(left: Poi, right: Poi) -> float:
    return (left.latitude - right.latitude) ** 2 + (left.longitude - right.longitude) ** 2
