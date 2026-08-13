"""OpenZLTravel 的行程持久化接口与 SQLite 实现。

数据库只保存请求快照和最终行程 JSON。MVP 不提前拆成几十张业务表，读取时由
Pydantic 负责结构校验，既保持简单，也便于未来迁移。
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.models import CandidateCatalog, City, Itinerary, Poi, TravelRequest, TripSummary


class TripRepository(Protocol):
    """行程持久化接口，业务服务只依赖这些最小能力。"""

    def save(self, itinerary: Itinerary, request: TravelRequest) -> None:
        """保存请求与完整行程快照。"""

        ...

    def get(self, trip_id: UUID) -> Itinerary | None:
        """按 ID 读取完整行程。"""

        ...

    def list(self) -> list[TripSummary]:
        """按创建时间倒序返回历史摘要。"""

        ...

    def delete(self, trip_id: UUID) -> bool:
        """删除行程，并返回是否命中记录。"""

        ...


class SqliteTripRepository:
    """基于标准库 sqlite3 的单用户行程仓库。"""

    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_table()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_table(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trips (
                    trip_id TEXT PRIMARY KEY,
                    destination TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    itinerary_json TEXT NOT NULL
                )
                """
            )

    def save(self, itinerary: Itinerary, request: TravelRequest) -> None:
        """保存完整快照；同一 ID 使用替换，便于后续支持重新生成。"""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO trips
                (trip_id, destination, start_date, end_date, summary, created_at,
                 request_json, itinerary_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(itinerary.trip_id),
                    itinerary.destination,
                    itinerary.start_date.isoformat(),
                    itinerary.end_date.isoformat(),
                    itinerary.summary,
                    itinerary.created_at.isoformat(),
                    request.model_dump_json(),
                    itinerary.model_dump_json(),
                ),
            )

    def get(self, trip_id: UUID) -> Itinerary | None:
        """读取已保存的完整行程，不存在时返回空值。"""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT itinerary_json FROM trips WHERE trip_id = ?", (str(trip_id),)
            ).fetchone()
        return Itinerary.model_validate_json(row["itinerary_json"]) if row else None

    def list(self) -> list[TripSummary]:
        """按创建时间倒序读取历史行程摘要。"""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trip_id, destination, start_date, end_date, summary, created_at
                FROM trips ORDER BY created_at DESC
                """
            ).fetchall()
        return [
            TripSummary(
                trip_id=UUID(row["trip_id"]),
                destination=row["destination"],
                start_date=datetime.fromisoformat(row["start_date"]).date(),
                end_date=datetime.fromisoformat(row["end_date"]).date(),
                summary=row["summary"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def delete(self, trip_id: UUID) -> bool:
        """删除指定行程，并返回是否实际删除。"""

        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM trips WHERE trip_id = ?", (str(trip_id),))
        return cursor.rowcount == 1


class CatalogRepository:
    """读取离线公开数据目录，不与行程历史数据库混用。"""

    SEARCH_RADIUS = 0.8

    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path)

    @property
    def available(self) -> bool:
        """判断目录是否已经由离线脚本生成。"""

        return self.database_path.is_file()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def resolve_city(self, destination: str) -> City:
        """按城市名或 GeoNames 中文别名查找城市坐标。"""

        if not self.available:
            raise LookupError("本地数据目录尚未生成")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT c.name, c.latitude, c.longitude
                FROM city_aliases a JOIN cities c ON c.city_id = a.city_id
                WHERE a.alias = ?
                ORDER BY a.population DESC
                LIMIT 1
                """,
                (destination.strip(),),
            ).fetchone()
        if row is None:
            raise LookupError(f"本地目录未覆盖城市：{destination}")
        return City(name=row["name"], latitude=row["latitude"], longitude=row["longitude"])

    def search_candidates(self, city: City) -> CandidateCatalog:
        """在城市中心附近读取三类 POI，供模型选择真实地点。"""

        if city.latitude is None or city.longitude is None:
            raise LookupError("本地城市缺少坐标")
        catalog = CandidateCatalog(
            attractions=self._search(city, "attraction", 12),
            restaurants=self._search(city, "restaurant", 12),
            hotels=self._search(city, "hotel", 8),
        )
        if not catalog.attractions:
            raise LookupError(f"本地目录没有找到城市附近的景点：{city.name}")
        return catalog

    def _search(self, city: City, category: str, limit: int) -> list[Poi]:
        latitude = city.latitude or 0
        longitude = city.longitude or 0
        radius = self.SEARCH_RADIUS
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT poi_id, name, address, category, latitude, longitude,
                       type_name, image_url
                FROM pois
                WHERE category = ?
                  AND latitude BETWEEN ? AND ?
                  AND longitude BETWEEN ? AND ?
                ORDER BY ((latitude - ?) * (latitude - ?)
                         + (longitude - ?) * (longitude - ?))
                LIMIT ?
                """,
                (
                    category,
                    latitude - radius,
                    latitude + radius,
                    longitude - radius,
                    longitude + radius,
                    latitude,
                    latitude,
                    longitude,
                    longitude,
                    limit,
                ),
            ).fetchall()
        return [
            Poi(
                id=row["poi_id"],
                name=row["name"],
                address=row["address"],
                category=row["category"],
                latitude=row["latitude"],
                longitude=row["longitude"],
                type_name=row["type_name"],
                image_url=row["image_url"],
            )
            for row in rows
        ]
