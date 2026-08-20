"""地点目录的可选展示信息补充边界。"""

from __future__ import annotations

from domain.models import CandidateCatalog, City


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
