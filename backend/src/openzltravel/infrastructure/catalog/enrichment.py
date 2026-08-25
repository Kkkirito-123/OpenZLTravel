"""地点目录的可选展示信息补充边界。"""

from __future__ import annotations

from openzltravel.domain.models import CandidateCatalog, City, ResolvedPlace

from .ranking import normalize_location_name


async def enrich_catalog(
    fallback: object | None, city: City, catalog: CandidateCatalog
) -> CandidateCatalog:
    """调用可选地图 Provider 补齐地址和图片，失败时保持本地事实不变。"""

    enrich = getattr(fallback, "enrich_catalog", None)
    if not callable(enrich):
        return catalog
    try:
        result = await enrich(city, catalog)
    except Exception:
        return catalog
    return result if isinstance(result, CandidateCatalog) else catalog


async def enrich_resolved_place(
    fallback: object | None,
    resolved: ResolvedPlace,
) -> ResolvedPlace:
    """仅在本地 POI 缺展示信息时，用同名近坐标 Provider 事实补齐。"""

    local = resolved.poi
    resolver = getattr(fallback, "resolve_place", None)
    if local is None or local.image_url or not callable(resolver):
        return resolved
    try:
        remote = await resolver(resolved.query)
    except Exception:
        return resolved
    if not isinstance(remote, ResolvedPlace) or not _same_place(resolved, remote):
        return resolved
    remote_poi = remote.poi
    if remote_poi is None:
        return resolved
    display = {"address": local.address or remote_poi.address, "image_url": remote_poi.image_url}
    return resolved.model_copy(update={"poi": local.model_copy(update=display)})


def _same_place(local: ResolvedPlace, remote: ResolvedPlace) -> bool:
    """补全只接受同名、同城且坐标接近的结果，避免图片串到同名异地 POI。"""

    left, right = local.poi, remote.poi
    if left is None or right is None:
        return False
    local_city, remote_city = local.city, remote.city
    same_city = normalize_location_name(local_city.name) == normalize_location_name(
        remote_city.name
    )
    if not same_city and local_city.adcode and remote_city.adcode:
        same_city = str(local_city.adcode)[:4] == str(remote_city.adcode)[:4]
    return (
        normalize_location_name(left.name) == normalize_location_name(right.name)
        and same_city
        and max(abs(left.latitude - right.latitude), abs(left.longitude - right.longitude)) <= 0.1
    )
