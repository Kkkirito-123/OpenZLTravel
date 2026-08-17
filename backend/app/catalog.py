"""PostgreSQL 公共地点目录查询。

本模块只负责把 ``catalog`` Schema 中的地点事实转换为应用模型。数据库未命中属于可降级
场景；连接失败属于基础设施故障，必须显式返回错误，避免一次故障放大成大量高德请求。
"""

from __future__ import annotations

import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any, Literal, cast

from psycopg import Error as PsycopgError
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from app.errors import CatalogUnavailableError
from app.models import CandidateCatalog, City, Poi

CITY_SQL = """
SELECT
    r.name,
    r.adcode,
    ST_Y(COALESCE(l.pointgeom, b.centergeom)) AS latitude,
    ST_X(COALESCE(l.pointgeom, b.centergeom)) AS longitude
FROM catalog.locationname AS n
JOIN catalog.region AS r ON r.regionid = n.locationid
JOIN catalog.location AS l ON l.locationid = r.regionid
LEFT JOIN catalog.boundary AS b ON b.regionid = r.regionid
WHERE n.normalizedname = %s
  AND r.level BETWEEN 1 AND 3
  AND COALESCE(l.pointgeom, b.centergeom) IS NOT NULL
ORDER BY
    (r.status = 'current') DESC,
    CASE r.level WHEN 2 THEN 3 WHEN 1 THEN 2 WHEN 3 THEN 1 ELSE 0 END DESC,
    (n.nametype = 'official') DESC,
    n.priority DESC,
    l.importance DESC,
    r.regionid
LIMIT 1
"""

POI_SQL = """
WITH center AS (
    SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326) AS pointgeom
), candidates AS (
    SELECT
        l.locationid,
        l.canonicalname,
        l.importance,
        p.address,
        p.category,
        p.typename,
        p.imageurl,
        ST_Y(l.pointgeom) AS latitude,
        ST_X(l.pointgeom) AS longitude,
        ST_Distance(l.pointgeom::geography, center.pointgeom::geography) AS distance
    FROM catalog.poi AS p
    JOIN catalog.location AS l ON l.locationid = p.locationid
    CROSS JOIN center
    WHERE p.category IN ('attraction', 'restaurant', 'hotel')
      AND l.pointgeom && ST_Expand(center.pointgeom, 1.0)
      AND ST_DWithin(l.pointgeom::geography, center.pointgeom::geography, 80000)
), ranked AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY category
        ORDER BY distance, importance DESC, locationid
    ) AS categoryrank
    FROM candidates
)
SELECT
    locationid,
    canonicalname,
    address,
    category,
    typename,
    imageurl,
    latitude,
    longitude
FROM ranked
WHERE categoryrank <= CASE category
    WHEN 'attraction' THEN 12
    WHEN 'restaurant' THEN 12
    ELSE 8
END
ORDER BY category, categoryrank
"""


class PostgresCatalogRepository:
    """使用有界连接池读取多人共享的 PostgreSQL 地点库。"""

    def __init__(
        self,
        database_url: str,
        min_size: int = 1,
        max_size: int = 4,
        timeout_seconds: float = 3,
        pool: Any | None = None,
    ) -> None:
        self.database_url = database_url
        self.timeout_seconds = timeout_seconds
        self._pool = (
            pool if pool is not None else self._create_pool(database_url, min_size, max_size)
        )

    @property
    def available(self) -> bool:
        """PostgreSQL 是已选择的权威目录，故障必须由查询方法显式报告。"""

        return True

    def resolve_city(self, destination: str) -> City:
        """按规范名解析当前行政区，并返回 WGS-84 中心点。"""

        normalized = normalize_location_name(destination)
        if not normalized:
            raise LookupError("目的地名称为空")
        row = self._fetch_one(CITY_SQL, (normalized,))
        if row is None:
            raise LookupError(f"地点库未覆盖城市：{destination}")
        return City(
            name=str(row["name"]),
            adcode=str(row["adcode"]),
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
        )

    def search_candidates(self, city: City) -> CandidateCatalog:
        """一次查询返回城市 80 公里内三类旅行 POI。"""

        if city.latitude is None or city.longitude is None:
            raise LookupError("地点库中的城市缺少坐标")
        rows = self._fetch_all(POI_SQL, (city.longitude, city.latitude))
        catalog = _candidate_catalog(rows)
        if not catalog.attractions:
            raise LookupError(f"地点库没有找到城市附近的景点：{city.name}")
        return catalog

    def readiness(self) -> dict[str, object]:
        """检查连接池和正式 Schema，不访问任何外部供应商。"""

        if self._pool is None:
            return {"status": "missing", "pool": {}}
        try:
            self._fetch_one("SELECT count(*) AS count FROM catalog.build", ())
        except CatalogUnavailableError:
            return {"status": "unavailable", "pool": self._pool_stats()}
        return {"status": "ready", "pool": self._pool_stats()}

    def close(self) -> None:
        """关闭连接池及其后台工作线程。"""

        if self._pool is not None:
            self._pool.close()

    def _create_pool(
        self, database_url: str, min_size: int, max_size: int
    ) -> ConnectionPool[Any] | None:
        if not database_url:
            return None
        pool_min_size = max(1, min_size)
        pool: ConnectionPool[Any] = ConnectionPool(
            conninfo=database_url,
            min_size=pool_min_size,
            max_size=max(pool_min_size, max_size),
            timeout=self.timeout_seconds,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        pool.open(wait=False)
        return pool

    def _fetch_one(self, query: str, parameters: tuple[Any, ...]) -> Any | None:
        rows = self._execute(query, parameters)
        return rows[0] if rows else None

    def _fetch_all(self, query: str, parameters: tuple[Any, ...]) -> list[Any]:
        return self._execute(query, parameters)

    def _execute(self, query: str, parameters: tuple[Any, ...]) -> list[Any]:
        if self._pool is None:
            raise CatalogUnavailableError("PostgreSQL 地点库尚未配置")
        try:
            with self._pool.connection(timeout=self.timeout_seconds) as connection:
                return list(connection.execute(query, parameters).fetchall())
        except (PsycopgError, PoolTimeout, OSError) as error:
            raise CatalogUnavailableError("PostgreSQL 地点库暂时不可用") from error

    def _pool_stats(self) -> dict[str, int]:
        if self._pool is None:
            return {}
        stats = self._pool.get_stats()
        keys = ("pool_size", "pool_available", "requests_waiting")
        return {key: int(stats.get(key, 0)) for key in keys}


class SqliteCatalogRepository:
    """显式回滚开关使用的旧 SQLite 目录，不作为正常运行路径。"""

    SEARCH_RADIUS = 0.8

    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path)

    @property
    def available(self) -> bool:
        """判断旧目录文件是否存在。"""

        return self.database_path.is_file()

    def resolve_city(self, destination: str) -> City:
        """按旧 SQLite 别名索引解析城市。"""

        if not self.available:
            raise LookupError("旧 SQLite 地点目录尚未生成")
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT c.name, c.latitude, c.longitude
                FROM city_aliases a JOIN cities c ON c.city_id = a.city_id
                WHERE a.alias = ? ORDER BY a.population DESC LIMIT 1
                """,
                (destination.strip(),),
            ).fetchone()
        if row is None:
            raise LookupError(f"旧目录未覆盖城市：{destination}")
        return City(name=row["name"], latitude=row["latitude"], longitude=row["longitude"])

    def search_candidates(self, city: City) -> CandidateCatalog:
        """从旧 SQLite 目录读取三类 POI。"""

        if city.latitude is None or city.longitude is None:
            raise LookupError("旧目录中的城市缺少坐标")
        catalog = _candidate_catalog(self._search(city))
        if not catalog.attractions:
            raise LookupError(f"旧目录没有找到城市附近的景点：{city.name}")
        return catalog

    def readiness(self) -> dict[str, object]:
        """返回回滚目录状态。"""

        return {"status": "ready" if self.available else "missing", "pool": {}}

    def close(self) -> None:
        """SQLite 查询按次连接，无常驻资源需要释放。"""

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _search(self, city: City) -> list[sqlite3.Row]:
        latitude = city.latitude or 0
        longitude = city.longitude or 0
        radius = self.SEARCH_RADIUS
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT poi_id AS locationid, name AS canonicalname, address, category,
                           type_name AS typename, image_url AS imageurl, latitude, longitude,
                           ROW_NUMBER() OVER (
                               PARTITION BY category
                               ORDER BY ((latitude - ?) * (latitude - ?)
                                       + (longitude - ?) * (longitude - ?))
                           ) AS categoryrank
                    FROM pois
                    WHERE latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?
                )
                SELECT * FROM ranked
                WHERE categoryrank <= CASE category
                    WHEN 'attraction' THEN 12 WHEN 'restaurant' THEN 12 ELSE 8 END
                """,
                (
                    latitude,
                    latitude,
                    longitude,
                    longitude,
                    latitude - radius,
                    latitude + radius,
                    longitude - radius,
                    longitude + radius,
                ),
            ).fetchall()
        return list(rows)


def normalize_location_name(value: str) -> str:
    """生成与离线构建过程一致的 Unicode 规范名称。"""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _candidate_catalog(rows: list[Any]) -> CandidateCatalog:
    groups: dict[str, list[Poi]] = {"attraction": [], "restaurant": [], "hotel": []}
    for row in rows:
        category = str(row["category"])
        groups[category].append(_postgres_poi(row))
    return CandidateCatalog(
        attractions=groups["attraction"],
        restaurants=groups["restaurant"],
        hotels=groups["hotel"],
    )


def _postgres_poi(row: Any) -> Poi:
    category = cast(Literal["attraction", "restaurant", "hotel"], str(row["category"]))
    return Poi(
        id=str(row["locationid"]),
        name=str(row["canonicalname"]),
        address=str(row["address"] or ""),
        category=category,
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        type_name=str(row["typename"] or ""),
        image_url=str(row["imageurl"]) if row["imageurl"] else None,
    )
