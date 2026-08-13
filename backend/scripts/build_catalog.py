"""构建 OpenZLTravel 的本地 POI 目录。

本脚本属于离线数据工具，不是后端运行时依赖。它读取 GeoNames 城市数据和
OpenStreetMap PBF，将公开数据统一写入 SQLite，运行时只查询这个本地目录。
原始数据保留在 data/raw，便于重新构建和核对来源；脚本不会请求高德 API。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import osmium
except ImportError as error:  # pragma: no cover - 仅在离线导入环境检查
    raise SystemExit(
        "缺少离线导入依赖 osmium，请在项目虚拟环境中执行：pip install osmium"
    ) from error


DATA_ROOT = Path(__file__).resolve().parents[1] / "data"
DEFAULT_OSM = DATA_ROOT / "raw" / "osm" / "china-latest.osm.pbf"
DEFAULT_GEONAMES = DATA_ROOT / "raw" / "geonames" / "CN.zip"
DEFAULT_OUTPUT = DATA_ROOT / "catalog.sqlite3"

# 下载地址和许可证写在构建代码中，保证生成目录可以追溯来源，而不是一个无来源的静态数据库。
OSM_SOURCE_URL = "https://download.geofabrik.de/asia/china-latest.osm.pbf"
OSM_LICENSE_URL = "https://www.openstreetmap.org/copyright"
GEONAMES_SOURCE_URL = "https://download.geonames.org/export/dump/CN.zip"
GEONAMES_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
CHINESE_TEXT = re.compile(r"[\u3400-\u9fff]")

ATTRACTION_TOURISM = {
    "attraction",
    "museum",
    "gallery",
    "zoo",
    "theme_park",
    "aquarium",
    "viewpoint",
    "artwork",
}
ATTRACTION_HISTORIC = {
    "castle",
    "monument",
    "memorial",
    "archaeological_site",
    "ruins",
    "temple",
    "city_gate",
}
ATTRACTION_LEISURE = {"park", "garden", "nature_reserve"}
RESTAURANT_AMENITIES = {"restaurant", "cafe", "fast_food", "food_court", "bar"}
HOTEL_TOURISM = {"hotel", "hostel", "guest_house", "motel", "apartment"}


def main() -> None:
    """解析原始文件并生成可查询的本地目录。"""

    arguments = _parse_arguments()
    _check_input_files(arguments.osm_file, arguments.geonames_file)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    connection = _open_catalog(arguments.output)
    try:
        city_count = _import_cities(connection, arguments.geonames_file)
        writer = PoiWriter(connection)
        handler = PoiHandler(writer)
        handler.apply_file(str(arguments.osm_file), locations=arguments.include_ways)
        writer.flush()
        _write_metadata(connection, city_count, writer.count, arguments.osm_file)
    finally:
        connection.close()
    print(f"目录构建完成：{arguments.output}")
    print(f"城市：{city_count}，POI：{writer.count}")


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 OpenZLTravel 本地 POI 目录")
    parser.add_argument("--osm-file", type=Path, default=DEFAULT_OSM)
    parser.add_argument("--geonames-file", type=Path, default=DEFAULT_GEONAMES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--include-ways",
        action="store_true",
        help="同时解析带坐标的 OSM 面要素；全国文件会明显增加内存占用",
    )
    return parser.parse_args()


def _check_input_files(osm_file: Path, geonames_file: Path) -> None:
    missing = [str(path) for path in (osm_file, geonames_file) if not path.exists()]
    if missing:
        raise SystemExit(f"缺少输入文件：{', '.join(missing)}")


def _open_catalog(output: Path) -> sqlite3.Connection:
    """创建目录表；目录是可重复生成的派生文件，不改动行程数据库。"""

    connection = sqlite3.connect(output)
    connection.executescript(
        """
        DROP TABLE IF EXISTS catalog_meta;
        DROP TABLE IF EXISTS city_aliases;
        DROP TABLE IF EXISTS cities;
        DROP TABLE IF EXISTS pois;

        CREATE TABLE catalog_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE cities (
            city_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            population INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE city_aliases (
            alias TEXT PRIMARY KEY,
            city_id TEXT NOT NULL,
            population INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (city_id) REFERENCES cities(city_id)
        );
        CREATE TABLE pois (
            poi_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL,
            category TEXT NOT NULL,
            address TEXT NOT NULL DEFAULT '',
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            type_name TEXT NOT NULL DEFAULT '',
            image_url TEXT,
            source TEXT NOT NULL,
            source_url TEXT NOT NULL,
            latitude_grid REAL NOT NULL,
            longitude_grid REAL NOT NULL,
            UNIQUE (category, name_key, latitude_grid, longitude_grid)
        );
        CREATE INDEX pois_category_location
            ON pois (category, latitude, longitude);
        CREATE INDEX city_aliases_city_id ON city_aliases (city_id);
        """
    )
    connection.commit()
    return connection


def _import_cities(connection: sqlite3.Connection, source: Path) -> int:
    """导入 GeoNames 聚居地，并建立中文名到坐标的别名索引。"""

    count = 0
    with zipfile.ZipFile(source) as archive, archive.open("CN.txt") as raw_file:
        for raw_line in raw_file:
            fields = raw_line.decode("utf-8").rstrip("\n").split("\t")
            if len(fields) < 19 or not _is_city(fields):
                continue
            city_id, name, latitude, longitude, population = _city_fields(fields)
            connection.execute(
                "INSERT INTO cities VALUES (?, ?, ?, ?, ?)",
                (city_id, name, latitude, longitude, population),
            )
            aliases = _city_aliases(fields)
            for alias in aliases:
                connection.execute(
                    """
                    INSERT INTO city_aliases(alias, city_id, population)
                    VALUES (?, ?, ?)
                    ON CONFLICT(alias) DO UPDATE SET
                        city_id = excluded.city_id,
                        population = excluded.population
                    WHERE excluded.population > city_aliases.population
                    """,
                    (alias, city_id, population),
                )
            count += 1
            if count % 5000 == 0:
                connection.commit()
    connection.commit()
    return count


def _is_city(fields: list[str]) -> bool:
    feature_class, feature_code = fields[6], fields[7]
    is_settlement = feature_class == "P" and (
        int(fields[14] or 0) > 0 or feature_code.startswith("PPLA")
    )
    return is_settlement or (feature_class == "A" and feature_code == "ADM2")


def _city_fields(fields: list[str]) -> tuple[str, str, float, float, int]:
    city_id, name = fields[0], fields[1]
    aliases = _city_aliases(fields)
    chinese_names = [item for item in aliases if CHINESE_TEXT.search(item)]
    display_name = next((item for item in chinese_names if item.endswith("市")), None)
    display_name = re.sub(r"市$", "", display_name or (chinese_names[0] if chinese_names else name))
    return city_id, display_name, float(fields[4]), float(fields[5]), int(fields[14] or 0)


def _city_aliases(fields: list[str]) -> list[str]:
    values = [fields[1], fields[2], *fields[3].split(",")]
    aliases: list[str] = []
    for value in values:
        alias = _decode_escaped_text(value.strip())
        if alias and len(alias) <= 80 and alias not in aliases:
            aliases.append(alias)
        if CHINESE_TEXT.search(alias):
            short_alias = re.sub(r"[省市县区]$", "", alias)
            if short_alias and short_alias not in aliases:
                aliases.append(short_alias)
    return aliases[:80]


def _decode_escaped_text(value: str) -> str:
    """还原 GeoNames 备用名中的 Unicode 转义，避免中文城市无法命中。"""

    return re.sub(
        r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})",
        lambda match: chr(int(match.group(1) or match.group(2), 16)),
        value,
    )


class PoiWriter:
    """将 OSM 解析结果批量写入 SQLite，并用网格键去除重复地点。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.pending: list[tuple[Any, ...]] = []
        self.count = 0

    def add(self, record: tuple[Any, ...]) -> None:
        self.pending.append(record)
        self.count += 1
        if len(self.pending) >= 5000:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        self.connection.executemany(
            """
            INSERT OR IGNORE INTO pois (
                poi_id, name, name_key, category, address, latitude, longitude,
                type_name, image_url, source, source_url, latitude_grid, longitude_grid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self.pending,
        )
        self.connection.commit()
        self.pending.clear()


class PoiHandler(osmium.SimpleHandler):
    """从 OSM 节点和带坐标的面要素中提取可用于旅行规划的 POI。"""

    def __init__(self, writer: PoiWriter) -> None:
        super().__init__()
        self.writer = writer

    def node(self, node: Any) -> None:
        record = _record_from_element(node, node.location, "node")
        if record:
            self.writer.add(record)

    def way(self, way: Any) -> None:
        location = _way_center(way.nodes)
        record = _record_from_element(way, location, "way")
        if record:
            self.writer.add(record)


def _record_from_element(element: Any, location: Any, element_type: str) -> tuple[Any, ...] | None:
    category = _category(element.tags)
    name = _tag(element.tags, "name:zh") or _tag(element.tags, "name")
    if not category or not name or not _valid_location(location):
        return None
    latitude, longitude = float(location.lat), float(location.lon)
    source_id = f"osm:{element_type}:{element.id}"
    return (
        source_id,
        name,
        _name_key(name),
        category,
        _address(element.tags),
        latitude,
        longitude,
        _type_name(element.tags),
        _image_url(_tag(element.tags, "image")),
        "OpenStreetMap",
        f"https://www.openstreetmap.org/{element_type}/{element.id}",
        round(latitude, 3),
        round(longitude, 3),
    )


def _category(tags: Any) -> str | None:
    tourism = _tag(tags, "tourism")
    amenity = _tag(tags, "amenity")
    historic = _tag(tags, "historic")
    leisure = _tag(tags, "leisure")
    if tourism in HOTEL_TOURISM:
        return "hotel"
    if amenity in RESTAURANT_AMENITIES:
        return "restaurant"
    if tourism in ATTRACTION_TOURISM or historic in ATTRACTION_HISTORIC:
        return "attraction"
    if leisure in ATTRACTION_LEISURE:
        return "attraction"
    return None


def _way_center(nodes: Iterable[Any]) -> Any | None:
    points = [node.location for node in nodes if _valid_location(node.location)]
    if not points:
        return None
    latitude = sum(float(point.lat) for point in points) / len(points)
    longitude = sum(float(point.lon) for point in points) / len(points)
    return _Point(latitude, longitude)


class _Point:
    """为 OSM 面要素提供与节点位置相同的最小坐标接口。"""

    def __init__(self, latitude: float, longitude: float) -> None:
        self.lat = latitude
        self.lon = longitude

    def valid(self) -> bool:
        """保持与 pyosmium Location 一致的有效性接口。"""

        return True


def _valid_location(location: Any) -> bool:
    if location is None:
        return False
    valid = getattr(location, "valid", None)
    return bool(valid() if callable(valid) else False)


def _tag(tags: Any, key: str) -> str:
    value = tags.get(key) if hasattr(tags, "get") else None
    return str(value or "").strip()


def _address(tags: Any) -> str:
    direct = _tag(tags, "addr:full")
    if direct:
        return direct
    parts = [
        _tag(tags, "addr:province"),
        _tag(tags, "addr:city"),
        _tag(tags, "addr:district"),
        _tag(tags, "addr:street"),
        _tag(tags, "addr:housenumber"),
    ]
    return "".join(item for item in parts if item)


def _type_name(tags: Any) -> str:
    return next(
        (
            _tag(tags, key)
            for key in ("tourism", "historic", "leisure", "amenity")
            if _tag(tags, key)
        ),
        "",
    )


def _image_url(value: str) -> str | None:
    parsed = urlsplit(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _name_key(value: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]", "", value).lower()


def _write_metadata(
    connection: sqlite3.Connection,
    city_count: int,
    poi_count: int,
    osm_file: Path,
) -> None:
    metadata = {
        "source": "OpenStreetMap + GeoNames",
        "osm_file": osm_file.name,
        "osm_source_url": OSM_SOURCE_URL,
        "osm_license_url": OSM_LICENSE_URL,
        "osm_license": "OpenStreetMap data © OpenStreetMap contributors, ODbL 1.0",
        "geonames_source_url": GEONAMES_SOURCE_URL,
        "geonames_license_url": GEONAMES_LICENSE_URL,
        "geonames_license": "GeoNames data, CC BY 4.0",
        "city_count": str(city_count),
        "poi_count_seen": str(poi_count),
    }
    connection.executemany(
        "INSERT INTO catalog_meta(key, value) VALUES (?, ?)", metadata.items()
    )
    connection.commit()


if __name__ == "__main__":
    main()
