# 离线数据目录

`build_catalog.py` 是一次性数据导入工具，不参与 FastAPI 运行时启动。

## 来源引用

- [OpenStreetMap 中国 PBF](https://download.geofabrik.de/asia/china-latest.osm.pbf)，许可证：[ODbL 1.0 / OpenStreetMap Copyright](https://www.openstreetmap.org/copyright)。
- [GeoNames 中国城市文件](https://download.geonames.org/export/dump/CN.zip)，许可证：[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。

## 数据来源

- OpenStreetMap 中国区域 PBF：POI，许可证为 ODbL 1.0。
- GeoNames `CN.zip`：城市名称、别名和坐标，许可证为 CC BY 4.0。

原始文件放在 `backend/data/raw/`，生成的 `backend/data/catalog.sqlite3` 是可重新构建的本地索引；两者都不提交到 Git。

## 构建

如需重新下载原始文件，在 `backend` 目录执行：

```powershell
.\scripts\download_public_data.ps1
```

离线导入环境先安装额外依赖：

```powershell
python -m pip install -r requirements-data.txt
```

在 `backend` 目录执行：

```powershell
python scripts/build_catalog.py
```

运行时会优先读取本地 POI；本地目录没有覆盖时，是否回退高德由 `ALLOW_AMAP_FALLBACK` 控制。
天气优先使用 Open-Meteo，普通步行和驾车采用本地估算；公交、地铁和明确选择的实时驾车
才调用高德，避免把 OSM 地物或直线距离误当成实时路线。
