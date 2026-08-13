# 本阶段：公开数据本地化与高德调用削减

## 阶段目标

本阶段解决重复调用高德城市和 POI 搜索接口导致的限流问题。这里采用的是“公开数据本地目录”，不是把高德返回结果伪装成缓存，也不改变天气和驾车路线的真实性约束。

## 已完成工作

1. 下载中国全量 OpenStreetMap PBF，作为景点、餐饮和酒店 POI 原始数据。
2. 下载 GeoNames 中国城市文件，作为城市名称、别名和坐标来源。
3. 增加 [download_public_data.ps1](backend/scripts/download_public_data.ps1)，支持断点续传和重复执行。
4. 增加 [build_catalog.py](backend/scripts/build_catalog.py)，流式解析 OSM 节点，提取三类 POI 并写入 SQLite。
5. 建立城市别名索引、POI 分类索引、坐标网格去重和来源元数据。
6. 增加 `CatalogRepository` 和 `HybridMapProvider`：本地优先，高德可选回退。
7. 增加数据来源、许可证、构建命令和运行边界说明。

## 数据来源与许可证

- [OpenStreetMap 中国 PBF](https://download.geofabrik.de/asia/china-latest.osm.pbf)：数据遵循 ODbL 1.0，归属说明见 [OpenStreetMap Copyright](https://www.openstreetmap.org/copyright)。
- [GeoNames CN.zip](https://download.geonames.org/export/dump/CN.zip)：数据遵循 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。

原始文件位于 `backend/data/raw/`，生成目录位于 `backend/data/catalog.sqlite3`。这些文件体积较大，均不提交到 Git；代码和脚本可以在重新下载后重建目录。

## 当前运行链路

```text
本地 GeoNames / OSM 目录
        ↓ 命中
城市解析 + POI 候选
        ↓ 未命中且允许回退
高德城市 + POI 搜索
        ↓
高德天气 + 高德驾车路线
        ↓
LLM 仅从真实候选 POI 中规划
```

本阶段已经减少城市和 POI 搜索请求，但天气和路线仍调用高德。OSM 道路数据暂未直接作为驾车路线，避免把道路几何或直线距离误当成真实导航结果。

## 本阶段验证

- 本地目录：16,122 个城市、103,959 个别名、89,300 条去重 POI。
- POI 分类：景点 24,008、餐饮 53,585、酒店 11,707。
- `西安`、`成都`、`厦门` 均可从本地目录解析并取得三类候选。
- 后端测试 25 项通过。
- Ruff 和 Mypy 通过。
- `/health` 与 `/api/trips` 联调正常。

## 尚未处理

- 天气结果缓存。
- 路线结果缓存或本地路线引擎。
- 并发限流、指数退避和请求去重。
- RAG、OpenZLAgent、多 Agent 和长任务恢复。
