"""目录层的城市推荐评分与名称归一化。

这个文件只处理确定性计算，不访问数据库、不调用模型，也不负责保存状态。
这样阅读 ``catalog/tool.py`` 时，可以把“查询真实数据”和“给城市排序”分开理解。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from domain.models import City, DestinationCandidate
from providers.base import stable_fact_id

REGION_GROUPS = {
    "华东": ("上海", "江苏", "浙江", "安徽", "福建", "江西", "山东"),
    "华南": ("广东", "广西", "海南"),
    "华北": ("北京", "天津", "河北", "山西", "内蒙古"),
    "华中": ("河南", "湖北", "湖南"),
    "西南": ("重庆", "四川", "贵州", "云南", "西藏"),
    "西北": ("陕西", "甘肃", "青海", "宁夏", "新疆"),
    "东北": ("辽宁", "吉林", "黑龙江"),
    "江浙沪": ("江苏", "浙江", "上海"),
    "长三角": ("江苏", "浙江", "上海", "安徽"),
}


@dataclass(frozen=True)
class DestinationProfile:
    """目录查询产生的城市覆盖与距离特征。"""

    city: City
    attraction_count: int
    restaurant_count: int
    hotel_count: int
    type_names: tuple[str, ...]
    distance_km: float | None


def normalize_location_name(value: str) -> str:
    """生成与独立目录构建器一致的 Unicode 规范名。"""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def region_names(region: str) -> tuple[str, ...]:
    """把“华东”等地区名展开为目录中的省市查询名。"""

    return REGION_GROUPS.get(normalize_location_name(region), (region,))


def destination_profile(row: Any) -> DestinationProfile:
    """把 PostGIS 聚合行转换成推荐评分输入。"""

    type_names = row.get("type_names", []) if isinstance(row, dict) else []
    return DestinationProfile(
        city=_city(row),
        attraction_count=int(row["attraction_count"] or 0),
        restaurant_count=int(row["restaurant_count"] or 0),
        hotel_count=int(row["hotel_count"] or 0),
        type_names=tuple(str(item) for item in type_names if item),
        distance_km=float(row["distance_km"]) if row["distance_km"] is not None else None,
    )


def rank_destinations(
    profiles: list[DestinationProfile],
    origin: str,
    preferences: list[str],
    limit: int,
) -> list[DestinationCandidate]:
    """按固定权重排序，并排除与出发地相同的城市。"""

    ranked = [_score_destination(profile, preferences) for profile in profiles]
    different = [
        item
        for item in ranked
        if normalize_location_name(item.city.name) != normalize_location_name(origin)
    ]
    usable = different or ranked
    usable.sort(key=lambda item: (-item.score, -item.attraction_count, item.city.name))
    return usable[:limit]


def _city(row: Any) -> City:
    return City(
        name=str(row["name"]),
        adcode=str(row["adcode"]).strip(),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
    )


def _score_destination(
    profile: DestinationProfile, preferences: list[str]
) -> DestinationCandidate:
    attraction = min(profile.attraction_count / 12, 1)
    preference, matched = _preference_score(profile.type_names, preferences)
    distance = _distance_score(profile.distance_km)
    amenities = (min(profile.restaurant_count / 12, 1) + min(profile.hotel_count / 8, 1)) / 2
    score = round(0.4 * attraction + 0.3 * preference + 0.2 * distance + 0.1 * amenities, 4)
    reasons = [f"景点候选 {profile.attraction_count} 个"]
    if matched:
        reasons.append(f"匹配偏好：{'、'.join(matched[:3])}")
    if profile.distance_km is not None:
        reasons.append(f"距出发地约 {round(profile.distance_km)} 公里")
    reasons.append(f"餐饮 {profile.restaurant_count} 个，住宿 {profile.hotel_count} 个")
    return DestinationCandidate(
        candidate_id=stable_fact_id("destination", profile.city.adcode or profile.city.name),
        city=profile.city,
        score=score,
        reasons=reasons[:4],
        attraction_count=profile.attraction_count,
        restaurant_count=profile.restaurant_count,
        hotel_count=profile.hotel_count,
    )


def _preference_score(
    type_names: tuple[str, ...], preferences: list[str]
) -> tuple[float, list[str]]:
    if not preferences:
        return 1.0, []
    normalized_types = [normalize_location_name(value) for value in type_names]
    matched = [
        preference
        for preference in preferences
        if any(
            normalize_location_name(preference) in type_name
            or type_name in normalize_location_name(preference)
            for type_name in normalized_types
            if type_name
        )
    ]
    return len(matched) / len(preferences), matched


def _distance_score(distance_km: float | None) -> float:
    if distance_km is None:
        return 0
    if distance_km <= 300:
        return 1
    if distance_km <= 800:
        return 0.75
    if distance_km <= 1500:
        return 0.5
    return 0.25
