"""PostGIS 地点目录、高德兜底与统一 Catalog 接口。"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Literal, Protocol, cast

from psycopg import Error as PsycopgError
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from domain.models import (
    CandidateCatalog,
    City,
    DestinationCandidate,
    Poi,
    ResolvedPlace,
)
from infrastructure.catalog.enrichment import enrich_catalog, enrich_resolved_place
from infrastructure.catalog.postgres_compat import execute_sync
from infrastructure.catalog.queries import (
    CITY_SQL,
    DESTINATION_SQL,
    PLACE_SQL,
    POI_SQL,
)
from infrastructure.catalog.ranking import (
    DestinationProfile,
    destination_profile,
    normalize_location_name,
    rank_destinations,
    region_names,
)
from infrastructure.providers.base import (
    AsyncTTLCache,
    CatalogUnavailableError,
    ProviderError,
    stable_key,
)
from infrastructure.providers.geo import http_url


class CatalogRepository(Protocol):
    """CatalogTool 依赖的最小本地目录接口。"""

    async def resolve_city(self, destination: str) -> City:
        """从本地目录解析城市。"""

    async def search_candidates(self, city: City) -> CandidateCatalog:
        """返回城市周边的三类 POI。"""

    async def resolve_place(self, query: str) -> ResolvedPlace:
        """从本地目录精确解析景点及其所属城市。"""

    async def destination_profiles(
        self, origin: str, region: str
    ) -> list[DestinationProfile]:
        """返回指定地区内的真实城市覆盖特征。"""


class CatalogFallback(Protocol):
    """本地目录未覆盖时允许使用的高德最小接口。"""

    async def resolve_city(self, destination: str) -> City:
        """通过高德确认城市。"""

    async def resolve_place(self, query: str) -> ResolvedPlace:
        """通过高德区分城市与具体地点。"""

    async def search_candidates(self, city: City) -> CandidateCatalog:
        """通过高德查询真实 POI。"""


class PostgresCatalogRepository:
    """使用有界连接池读取 PostGIS 地点目录，并兼容 Windows 同步连接。"""

    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
        timeout_seconds: float = 3,
        pool: Any | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._sync_database_url = (
            database_url if sys.platform == "win32" and pool is None else None
        )
        self._pool = (
            pool
            if pool is not None
            else None
            if self._sync_database_url is not None
            else self._create_pool(database_url, min_size, max_size)
        )
        self._owns_pool = pool is None and self._pool is not None
        self._opened = False
        self._open_lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        """返回是否配置了本地目录连接池或 Windows 同步兼容连接。"""

        return self._pool is not None or self._sync_database_url is not None

    async def resolve_city(self, destination: str) -> City:
        """按官方名、简称或别名解析 WGS-84 城市中心。"""

        normalized = normalize_location_name(destination)
        if not normalized:
            raise LookupError("目的地名称为空")
        rows = await self._execute(CITY_SQL, (normalized,))
        if not rows:
            raise LookupError(f"地点目录未覆盖城市：{destination}")
        return _city(rows[0])

    async def search_candidates(self, city: City) -> CandidateCatalog:
        """一次查询返回城市 80 公里内的景点、餐饮和酒店。"""

        if city.latitude is None or city.longitude is None:
            raise LookupError("城市缺少可用坐标")
        rows = await self._execute(POI_SQL, (city.longitude, city.latitude))
        catalog = _candidate_catalog(rows)
        if not catalog.attractions:
            raise LookupError(f"地点目录没有找到{city.name}附近的景点")
        return catalog

    async def resolve_place(self, query: str) -> ResolvedPlace:
        """按规范名或别名精确解析景点，并返回所属地级市。"""

        normalized = normalize_location_name(query)
        if not normalized:
            raise LookupError("地点名称为空")
        rows = await self._execute(PLACE_SQL, (normalized,))
        if not rows:
            raise LookupError(f"地点目录未覆盖景点：{query}")
        row = rows[0]
        city = City(
            name=str(row["cityname"]),
            adcode=str(row["cityadcode"]).strip(),
            latitude=float(row["citylatitude"]),
            longitude=float(row["citylongitude"]),
        )
        return ResolvedPlace(
            query=query,
            city=city,
            poi=_poi(row, "attraction"),
        )

    async def destination_profiles(
        self, origin: str, region: str
    ) -> list[DestinationProfile]:
        """读取地区内城市的 POI 覆盖、类型和相对出发地距离。"""

        rows: list[Any] = []
        for region_name in region_names(region):
            parameters = (
                normalize_location_name(region_name),
                normalize_location_name(origin),
            )
            rows.extend(await self._execute(DESTINATION_SQL, parameters))
        if not rows:
            raise LookupError(f"地点目录无法为“{region}”生成城市候选")
        profiles = [destination_profile(row) for row in rows]
        return list({item.city.adcode or item.city.name: item for item in profiles}.values())

    async def aclose(self) -> None:
        """关闭本实例创建的 PostgreSQL 连接池。"""

        if self._owns_pool and self._pool is not None:
            await self._pool.close()

    def _create_pool(
        self, database_url: str, min_size: int, max_size: int
    ) -> AsyncConnectionPool[Any] | None:
        if not database_url:
            return None
        lower = max(1, min_size)
        return AsyncConnectionPool(
            conninfo=database_url,
            min_size=lower,
            max_size=max(lower, max_size),
            timeout=self.timeout_seconds,
            kwargs={"row_factory": dict_row},
            open=False,
        )

    async def _execute(self, query: str, parameters: tuple[Any, ...]) -> list[Any]:
        if self._sync_database_url is not None:
            try:
                return await asyncio.to_thread(
                    execute_sync,
                    self._sync_database_url,
                    query,
                    parameters,
                    self.timeout_seconds,
                )
            except (PsycopgError, OSError) as error:
                raise CatalogUnavailableError() from error
        if self._pool is None:
            raise CatalogUnavailableError("尚未配置 PostgreSQL 地点目录")
        try:
            await self._ensure_open()
            async with self._pool.connection(timeout=self.timeout_seconds) as connection:
                cursor = await connection.execute(query, parameters)
                return list(await cursor.fetchall())
        except (PsycopgError, PoolTimeout, OSError) as error:
            raise CatalogUnavailableError() from error

    async def _ensure_open(self) -> None:
        if not self._owns_pool or self._opened:
            return
        async with self._open_lock:
            if self._opened:
                return
            pool = self._pool
            if pool is None:
                raise CatalogUnavailableError("尚未配置 PostgreSQL 地点目录")
            await pool.open(wait=False)
            self._opened = True


class CatalogTool:
    """向图提供统一城市、POI 和目的地推荐接口。"""

    def __init__(
        self,
        repository: CatalogRepository | None,
        fallback: CatalogFallback | None = None,
        *,
        cache: AsyncTTLCache | None = None,
    ) -> None:
        self.repository = repository
        self.fallback = fallback
        self.cache = cache or AsyncTTLCache(max_entries=128)

    async def resolve_city(self, destination: str) -> City:
        """本地目录优先解析；仅“未覆盖”允许高德兜底。"""

        key = stable_key("city", normalize_location_name(destination))
        if cached := await self.cache.get(key):
            return cast(City, cached)
        city = await self._resolve_uncached(destination)
        await self.cache.set(key, city, 86400)
        return city

    async def resolve_place(self, query: str) -> ResolvedPlace:
        """本地城市优先；非城市表达交给事实 Provider 解析为具体地点。"""

        key = stable_key("place", normalize_location_name(query))
        if cached := await self.cache.get(key):
            return cast(ResolvedPlace, cached).model_copy(update={"query": query})
        resolved = await self._resolve_place_uncached(query)
        await self.cache.set(key, resolved, 86400)
        return resolved

    async def search_candidates(self, city: City) -> CandidateCatalog:
        """本地目录优先读取 POI；未覆盖时才使用高德。"""

        key = stable_key("catalog", city.adcode or city.name)
        if cached := await self.cache.get(key):
            return cast(CandidateCatalog, cached)
        catalog = await self._candidates_uncached(city)
        await self.cache.set(key, catalog, 3600)
        return catalog

    async def recommend_destinations(
        self,
        origin: str,
        region: str,
        preferences: list[str],
        limit: int = 5,
    ) -> list[DestinationCandidate]:
        """按固定权重返回最多 5 个真实城市，预算不参与排名。"""

        if self.repository is None:
            raise ProviderError(
                "destination_recommendation_unavailable", "目的地推荐需要本地地点目录"
            )
        safe_limit = min(5, max(1, limit))
        key = stable_key("destinations", origin, region, sorted(preferences), safe_limit)
        if cached := await self.cache.get(key):
            return cast(list[DestinationCandidate], cached)
        profiles = await self.repository.destination_profiles(origin, region)
        candidates = rank_destinations(profiles, origin, preferences, safe_limit)
        await self.cache.set(key, candidates, 3600)
        return candidates

    async def _resolve_uncached(self, destination: str) -> City:
        if self.repository is not None:
            try:
                return await self.repository.resolve_city(destination)
            except LookupError:
                pass
        if self.fallback is not None:
            return await self.fallback.resolve_city(destination)
        raise ProviderError("city_not_found", f"无法确认目的地“{destination}”")

    async def _resolve_place_uncached(self, query: str) -> ResolvedPlace:
        if self.repository is not None:
            try:
                city = await self.repository.resolve_city(query)
                return ResolvedPlace(query=query, city=city)
            except LookupError:
                pass
            try:
                resolved = await self.repository.resolve_place(query)
                return await enrich_resolved_place(self.fallback, resolved)
            except LookupError:
                pass
        if self.fallback is not None:
            return await self.fallback.resolve_place(query)
        raise ProviderError("place_not_found", f"无法确认地点“{query}”")

    async def _candidates_uncached(self, city: City) -> CandidateCatalog:
        if self.repository is not None:
            try:
                catalog = await self.repository.search_candidates(city)
                catalog = await enrich_catalog(self.fallback, city, catalog)
                return catalog
            except LookupError:
                pass
        if self.fallback is not None:
            return await self.fallback.search_candidates(city)
        raise ProviderError("catalog_not_found", f"未找到{city.name}的真实地点候选")


def _city(row: Any) -> City:
    return City(
        name=str(row["name"]),
        adcode=str(row["adcode"]).strip(),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
    )


def _candidate_catalog(rows: list[Any]) -> CandidateCatalog:
    groups: dict[str, list[Poi]] = {"attraction": [], "restaurant": [], "hotel": []}
    for row in rows:
        category = str(row["category"])
        if category in groups:
            groups[category].append(_poi(row, cast(Any, category)))
    return CandidateCatalog(
        attractions=groups["attraction"],
        restaurants=groups["restaurant"],
        hotels=groups["hotel"],
    )


def _poi(row: Any, category: Literal["attraction", "restaurant", "hotel"]) -> Poi:
    type_name = str(row["typename"] or "")
    return Poi(
        id=f"poi:catalog:{row['locationid']}",
        name=str(row["canonicalname"]),
        address=str(row["address"] or ""),
        category=category,
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        type_name=type_name,
        image_url=http_url(row["imageurl"]),
        tags=[item for item in type_name.split(";") if item],
    )
