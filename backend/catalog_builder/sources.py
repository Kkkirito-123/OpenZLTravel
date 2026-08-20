"""公共地点目录的四类原始数据流式解析器。

解析器只把来源字段变成可信的中间记录，不负责数据库事务和来源优先级合并。
全国文件始终逐行或逐元素读取，避免把百万级数据一次放进内存。
"""

from __future__ import annotations

import csv
import gc
import hashlib
import json
import math
import re
import sys
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from .geometry import (
    adcode_level,
    geoname_uuid,
    normalize_adcode,
    normalize_name,
    osm_uuid,
    parse_gcj02_multipolygon,
    parse_gcj02_point,
    point_ewkt,
    preferred_geoname,
    region_uuid,
    short_region_name,
    stable_uuid,
    valid_http_url,
)

ROOT_ADCODE = "000000000000"
ROOT_UUID = stable_uuid("region:china")
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


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    """写入 source 表的一条可追溯来源。"""

    code: str
    name: str
    version: str
    url: str
    license_name: str
    license_url: str | None
    checksum: str


@dataclass(frozen=True, slots=True)
class RegionRaw:
    """合并前的一条行政区来源记录。"""

    regionid: UUID
    sourcecode: str
    sourcekey: str
    adcode: str
    parentid: UUID | None
    level: int
    path: str
    name: str
    shortname: str
    pinyin: str
    initial: str
    status: str
    priority: int


@dataclass(frozen=True, slots=True)
class NameRaw:
    """地点的一个可检索名称。"""

    name: str
    normalized: str
    kind: str
    language: str
    priority: int


@dataclass(frozen=True, slots=True)
class GeoRaw:
    """GeoNames 的完整地理实体中间记录。"""

    locationid: UUID
    geonameid: int
    canonicalname: str
    point: str
    importance: float
    asciiname: str
    featureclass: str
    featurecode: str
    countrycode: str
    admin1: str
    admin2: str
    admin3: str
    admin4: str
    population: int
    elevation: int | None
    dem: int | None
    timezone: str
    modifiedon: date | None
    names: tuple[NameRaw, ...]


@dataclass(frozen=True, slots=True)
class BoundaryRaw:
    """AreaCity 三级中心点和行政边界中间记录。"""

    adcode: str
    sourcekey: str
    center: str | None
    polygon: str | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PoiRaw:
    """从 OSM 节点或区域提取的旅行 POI。"""

    locationid: UUID
    elementtype: str
    elementid: int
    canonicalname: str
    category: str
    typename: str
    address: str
    imageurl: str | None
    sourceadcode: str | None
    sourceurl: str
    point: str | None
    areawkb: str | None
    names: tuple[NameRaw, ...]


def source_definitions(
    modood_archive: Path,
    area_files: Sequence[Path],
    geonames_archive: Path,
    osm_file: Path,
) -> tuple[SourceDefinition, ...]:
    """根据实际输入文件生成来源元数据和校验哈希。"""

    return (
        SourceDefinition(
            "system",
            "OpenZLTravel 系统节点",
            "1",
            "internal://openzltravel/catalog",
            "项目内部数据",
            None,
            "0" * 64,
        ),
        SourceDefinition(
            "modood",
            "Modood 中国五级行政区划",
            "2.7.0 / 2023",
            "https://github.com/modood/Administrative-divisions-of-China",
            "WTFPL 2.0",
            "https://github.com/modood/Administrative-divisions-of-China/blob/master/LICENSE",
            file_sha256(modood_archive),
        ),
        SourceDefinition(
            "areacity",
            "AreaCity 行政区划与三级边界",
            "2025.251231.260403",
            "https://github.com/xiangyuecn/AreaCity-JsSpider-StatsGov",
            "项目说明允许免费使用，未声明标准 SPDX 许可证",
            "https://github.com/xiangyuecn/AreaCity-JsSpider-StatsGov",
            combined_sha256(area_files),
        ),
        SourceDefinition(
            "geonames",
            "GeoNames 中国地名",
            "CN dump",
            "https://download.geonames.org/export/dump/CN.zip",
            "CC BY 4.0",
            "https://creativecommons.org/licenses/by/4.0/",
            file_sha256(geonames_archive),
        ),
        SourceDefinition(
            "osm",
            "OpenStreetMap 中国数据",
            osm_file.name,
            "https://download.geofabrik.de/asia/china-latest.osm.pbf",
            "ODbL 1.0",
            "https://www.openstreetmap.org/copyright",
            file_sha256(osm_file),
        ),
    )


def root_region() -> RegionRaw:
    """创建所有省级节点共同指向的中国根节点。"""

    return RegionRaw(
        regionid=ROOT_UUID,
        sourcecode="system",
        sourcekey="china",
        adcode=ROOT_ADCODE,
        parentid=None,
        level=0,
        path="cn",
        name="中国",
        shortname="中国",
        pinyin="zhong guo",
        initial="zg",
        status="current",
        priority=1000,
    )


def iter_modood_regions(directory: Path) -> Iterator[RegionRaw]:
    """按省、地、县、乡、村顺序流式读取 Modood 五级树。"""

    specs = (
        ("provinces.csv", 1, None, ("code",)),
        ("cities.csv", 2, "provinceCode", ("provinceCode", "code")),
        ("areas.csv", 3, "cityCode", ("provinceCode", "cityCode", "code")),
        (
            "streets.csv",
            4,
            "areaCode",
            ("provinceCode", "cityCode", "areaCode", "code"),
        ),
        (
            "villages.csv",
            5,
            "streetCode",
            ("provinceCode", "cityCode", "areaCode", "streetCode", "code"),
        ),
    )
    for filename, level, parent_column, path_columns in specs:
        with (directory / filename).open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                yield _modood_region(row, level, parent_column, path_columns)


def iter_areacity_regions(source: Path) -> Iterator[RegionRaw]:
    """读取 AreaCity 一至四级树，并排除其非中国的“国外”节点。"""

    paths: dict[str, str] = {"0": "cn"}
    with source.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["ext_id"] == "0" or row["id"] == "91":
                continue
            parent_key = row["pid"]
            parent_path = paths.get(parent_key)
            if parent_path is None:
                raise ValueError(f"AreaCity 父节点尚未出现：{parent_key}")
            path = f"{parent_path}.r{row['id']}"
            paths[row["id"]] = path
            adcode = normalize_adcode(row["ext_id"])
            parentid = ROOT_UUID if row["deep"] == "0" else region_uuid(row["pid"])
            yield RegionRaw(
                regionid=region_uuid(adcode),
                sourcecode="areacity",
                sourcekey=row["ext_id"],
                adcode=adcode,
                parentid=parentid,
                level=adcode_level(row["id"]),
                path=path,
                name=row["ext_name"].strip() or row["name"].strip(),
                shortname=row["name"].strip(),
                pinyin=row["pinyin"].strip(),
                initial=row["pinyin_prefix"].strip(),
                status="current",
                priority=100,
            )


def iter_geonames(source: Path) -> Iterator[GeoRaw]:
    """读取 GeoNames CN.txt 的全部有效坐标记录。"""

    with zipfile.ZipFile(source) as archive, archive.open("CN.txt") as raw_stream:
        for raw_line in raw_stream:
            fields = raw_line.decode("utf-8").rstrip("\n").split("\t")
            if len(fields) < 19:
                continue
            record = _geoname_record(fields)
            if record is not None:
                yield record


def iter_boundaries(source: Path) -> Iterator[BoundaryRaw]:
    """逐行转换 AreaCity 边界；单条损坏只记录错误，不丢失行政节点。"""

    csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
    with source.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["id"] in {"0", "91"}:
                continue
            try:
                center = parse_gcj02_point(row["geo"])
                polygon = parse_gcj02_multipolygon(row["polygon"])
                if row["polygon"] not in {"", "EMPTY"} and polygon is None:
                    raise ValueError("边界文本不包含有效多边形")
                yield BoundaryRaw(
                    adcode=normalize_adcode(row["id"]),
                    sourcekey=row["id"],
                    center=center,
                    polygon=polygon,
                )
            except (TypeError, ValueError) as error:
                yield BoundaryRaw(
                    adcode=normalize_adcode(row["id"]),
                    sourcekey=row["id"],
                    center=None,
                    polygon=None,
                    error=str(error)[:300],
                )


def iter_osm_pois(source: Path, index_path: Path) -> Iterator[PoiRaw]:
    """流式提取 OSM 节点与多边形旅行 POI，节点缓存落盘。"""

    try:
        import osmium
    except ImportError as error:  # pragma: no cover - 由离线环境检查
        raise RuntimeError(
            "缺少 osmium，请先执行：python -m pip install -e '.[catalog]'"
        ) from error

    if index_path.exists():
        index_path.unlink()
    # 中国全量 OSM 的节点编号很稀疏，dense_file_array 会按最大编号分配巨型文件。
    # sparse_file_array 同样把节点索引落盘，但磁盘占用与实际节点数更接近。
    storage = f"sparse_file_array,{index_path}"
    factory = osmium.geom.WKBFactory()
    processor = osmium.FileProcessor(str(source)).with_locations(storage).with_areas()
    element: Any = None
    record: PoiRaw | None = None
    try:
        for element in processor:
            if isinstance(element, osmium.osm.Node):
                record = _node_poi(element)
            elif isinstance(element, osmium.osm.Area):
                record = _area_poi(element, factory)
            else:
                record = None
            if record is not None:
                yield record
    finally:
        # FileProcessor 没有公开 close；主动断开其 C++ 存储引用，避免 Windows
        # 在生成器结束后仍锁住 sparse_file_array 临时文件。
        processor._node_store = None
        processor._area_handler = None
        delattr(processor, "_thread_pool")
        del processor, factory, element, record
        gc.collect()


def file_sha256(path: Path) -> str:
    """流式计算单个来源文件的 SHA256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def combined_sha256(paths: Sequence[Path]) -> str:
    """将多个有序输入文件合并为一个可重复的来源哈希。"""

    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(file_sha256(path).encode("ascii"))
    return digest.hexdigest()


def names_json(names: Sequence[NameRaw]) -> str:
    """序列化名称列表，供 PostgreSQL 临时表批量展开。"""

    return json.dumps(
        [
            {
                "name": item.name,
                "normalized": item.normalized,
                "kind": item.kind,
                "language": item.language,
                "priority": item.priority,
            }
            for item in names
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _modood_region(
    row: Mapping[str, str],
    level: int,
    parent_column: str | None,
    path_columns: Sequence[str],
) -> RegionRaw:
    adcode = normalize_adcode(row["code"])
    labels = [f"r{row[column]}" for column in path_columns]
    parentid = ROOT_UUID if parent_column is None else region_uuid(row[parent_column])
    name = row["name"].strip()
    return RegionRaw(
        regionid=region_uuid(adcode),
        sourcecode="modood",
        sourcekey=row["code"],
        adcode=adcode,
        parentid=parentid,
        level=level,
        path=".".join(("cn", *labels)),
        name=name,
        shortname=short_region_name(name),
        pinyin="",
        initial="",
        status="undetermined" if level == 5 else "legacy",
        priority=10,
    )


def _geoname_record(fields: list[str]) -> GeoRaw | None:
    try:
        geonameid = int(fields[0])
        latitude, longitude = float(fields[4]), float(fields[5])
        population = int(fields[14] or 0)
    except ValueError:
        return None
    aliases = _unique_aliases(fields[1], fields[2], fields[3])
    canonical = preferred_geoname(fields[1], aliases)
    names = _geoname_names(fields[1], fields[2], aliases)
    importance = round(math.log10(max(0, population) + 1) * 10, 4)
    return GeoRaw(
        locationid=geoname_uuid(geonameid),
        geonameid=geonameid,
        canonicalname=canonical,
        point=point_ewkt(latitude, longitude),
        importance=importance,
        asciiname=fields[2],
        featureclass=fields[6],
        featurecode=fields[7],
        countrycode=fields[8] or "CN",
        admin1=fields[10],
        admin2=fields[11],
        admin3=fields[12],
        admin4=fields[13],
        population=population,
        elevation=_optional_int(fields[15]),
        dem=_optional_int(fields[16]),
        timezone=fields[17],
        modifiedon=_optional_date(fields[18]),
        names=names,
    )


def _unique_aliases(name: str, ascii_name: str, raw_aliases: str) -> tuple[str, ...]:
    values = (name, ascii_name, *raw_aliases.split(","))
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        alias = _decode_escaped_text(value.strip())
        normalized = normalize_name(alias)
        if not alias or not normalized or normalized in seen:
            continue
        aliases.append(alias)
        seen.add(normalized)
    return tuple(aliases)


def _geoname_names(name: str, ascii_name: str, aliases: Sequence[str]) -> tuple[NameRaw, ...]:
    records = [_name_record(name, "official", "", 80)]
    seen = {records[0].normalized}
    if ascii_name and normalize_name(ascii_name) != normalize_name(name):
        record = _name_record(ascii_name, "alternate", "", 40)
        records.append(record)
        seen.add(record.normalized)
    for alias in aliases:
        normalized = normalize_name(alias)
        if normalized in seen:
            continue
        priority = 60 if CHINESE_TEXT.search(alias) else 30
        records.append(NameRaw(alias, normalized, "alternate", "", priority))
        seen.add(normalized)
    return tuple(records)


def _name_record(name: str, kind: str, language: str, priority: int) -> NameRaw:
    return NameRaw(name, normalize_name(name), kind, language, priority)


def _node_poi(node: Any) -> PoiRaw | None:
    tags = node.tags
    category = _poi_category(tags)
    name = _tag(tags, "name:zh") or _tag(tags, "name")
    if not category or not name or not node.location.valid():
        return None
    elementid = int(node.id)
    return _poi_record(
        tags,
        "node",
        elementid,
        name,
        category,
        point_ewkt(float(node.location.lat), float(node.location.lon)),
        None,
    )


def _area_poi(area: Any, factory: Any) -> PoiRaw | None:
    tags = area.tags
    category = _poi_category(tags)
    name = _tag(tags, "name:zh") or _tag(tags, "name")
    if not category or not name:
        return None
    elementtype = "way" if area.from_way() else "relation"
    elementid = int(area.orig_id())
    try:
        areawkb = str(factory.create_multipolygon(area))
    except (RuntimeError, ValueError):
        return None
    return _poi_record(tags, elementtype, elementid, name, category, None, areawkb)


def _poi_record(
    tags: Any,
    elementtype: str,
    elementid: int,
    name: str,
    category: str,
    point: str | None,
    areawkb: str | None,
) -> PoiRaw:
    names = [_name_record(name, "official", "zh" if CHINESE_TEXT.search(name) else "", 80)]
    english = _tag(tags, "name:en")
    if english and normalize_name(english) != normalize_name(name):
        names.append(_name_record(english, "alternate", "en", 40))
    return PoiRaw(
        locationid=osm_uuid(elementtype, elementid),
        elementtype=elementtype,
        elementid=elementid,
        canonicalname=name,
        category=category,
        typename=_poi_type(tags),
        address=_address(tags),
        imageurl=valid_http_url(_tag(tags, "image")),
        sourceadcode=_source_adcode(tags),
        sourceurl=f"https://www.openstreetmap.org/{elementtype}/{elementid}",
        point=point,
        areawkb=areawkb,
        names=tuple(names),
    )


def _poi_category(tags: Any) -> str | None:
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
    return "attraction" if leisure in ATTRACTION_LEISURE else None


def _poi_type(tags: Any) -> str:
    values = (_tag(tags, key) for key in ("tourism", "historic", "leisure", "amenity"))
    return next((value for value in values if value), "")


def _source_adcode(tags: Any) -> str | None:
    """读取 OSM 明确给出的国标行政代码，不根据名称或坐标猜测。"""

    value = _tag(tags, "ref:GB:adcode") or _tag(tags, "adcode")
    if not value.isdigit() or len(value) not in {2, 4, 6, 9, 12}:
        return None
    return normalize_adcode(value)


def _tag(tags: Any, key: str) -> str:
    value = tags.get(key) if hasattr(tags, "get") else None
    return str(value or "").strip()


def _address(tags: Any) -> str:
    direct = _tag(tags, "addr:full")
    if direct:
        return direct
    keys = ("addr:province", "addr:city", "addr:district", "addr:street", "addr:housenumber")
    return "".join(_tag(tags, key) for key in keys)


def _decode_escaped_text(value: str) -> str:
    return re.sub(
        r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})",
        lambda match: chr(int(match.group(1) or match.group(2), 16)),
        value,
    )


def _optional_int(value: str) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _optional_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None
