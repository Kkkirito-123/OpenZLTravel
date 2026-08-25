# OpenZLTravel

OpenZLTravel 是一个“独立旅行交流助手 + 轻量 Travel LangGraph”的单目的地旅行规划项目。

## 工作方式

1. 用户只面对一个 Vue AI 对话页面，不填写需求表单。
2. Assistant Service 使用 LangChain `create_agent` 理解自然语言，并调用 Catalog、12306、
   RollingGo 和天气 Provider。
3. 城市、景点、车次和酒店以卡片展示；点击和自然语言选择走同一服务端事实 ID 校验。
4. 用户说“开始规划”后，Assistant 刷新时间敏感事实并签发短时 `TravelOrderToken`。
5. 前端新建 Travel Thread/Run，LangGraph 验证工单、生成每日行程、查询最终路线、计算预算。
6. 用户只在最终 `route_preview` 确认或做受限修改；确认后按 `user_id + order_id` 幂等保存。

完整边界见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 目录

```text
backend/src/
  assistant/          LangChain 对话服务、工具、SSE 和工单交接
  travel_graph/       LangGraph 状态、节点、API、Checkpoint 和保存
  domain/             框架无关的领域模型、确定性规划和校验
  infrastructure/
    catalog/          本地 PostGIS POI 查询
    providers/        车票、酒店、天气、地图和路线 Provider
  runtime/            配置、身份、依赖装配、Protocol 和签名 Token

frontend/src/features/
  assistant/          对话页面、卡片、SSE 和会话状态
  planning/           Graph Run、路线确认和行程展示
  trips/              历史行程
```

## 本地开发

后端默认 `PROVIDER_MODE=fake` 时不需要外部 Provider：

```powershell
cd backend
python -m pip install -e ".[dev]"
python -m uvicorn assistant.app:app --app-dir src --port 2030
```

另一个终端启动 LangGraph Agent Server：

```powershell
cd backend
langgraph dev --port 2024
```

前端：

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

浏览器访问 `http://127.0.0.1:5173`。

## Docker 四服务

复制本机配置文件并填写必要密钥与 PostGIS 密码：

```powershell
Copy-Item backend/.env.example backend/.env
Copy-Item backend/.env.catalog.example backend/.env.catalog.local
.\start.ps1 up
```

Compose 包含：

- `catalogdb`：PostGIS 地点目录；
- `assistant`：端口 `2030`，负责对话、事实和工单；
- `agent`：端口 `2024`，只运行 `travel` 图和历史 API；
- `frontend`：端口 `5173`，代理两个后端。

## 公开接口

Assistant：

```text
GET  /ok
POST /api/assistant/turn   SSE
```

Agent Server 自定义业务接口：

```text
POST   /api/auth/anonymous
GET    /api/trips
GET    /api/trips/{trip_id}
DELETE /api/trips/{trip_id}
```

Thread、Run、Checkpoint 使用 LangGraph Agent Server 标准接口。TravelGraph 输入固定为：

```json
{"order_token":"assistant-issued-token"}
```

唯一恢复载荷为：

```json
{"kind":"route_preview","action":"message","text":"把西湖放到第二天"}
```

或：

```json
{"kind":"route_preview","action":"confirm"}
```

## 验证

```powershell
cd backend
python -m ruff check src tests catalog_builder
python -m mypy
python -m pytest -q

cd ../frontend
npm.cmd test
npm.cmd run build

cd ..
docker compose --env-file backend/.env --env-file backend/.env.catalog.local config
```

## 固定 Benchmark

后端包含 30 个固定用例，分为离线功能层和可选 LangSmith 质量层。离线层使用 Fake Provider
和 Replay Model，不访问铁路、酒店、天气或地图网络：

```powershell
cd backend
python -m benchmarks.run --suite functional --report reports/functional.json
python -m benchmarks.run --suite all --report reports/latest.json
```

配置 `LANGSMITH_API_KEY`（或 `LANGCHAIN_API_KEY`）和当前 LLM 密钥后，`quality` 层会以固定
数据运行 LangSmith 评估；缺少密钥时会明确标记 `skipped`，不会影响离线结果。报告同时写入
JSON 和 Markdown，数据集位于 `backend/benchmarks/data/`。

`.env`、Provider Token、数据库 volume 和构建产物均是本机数据，不应提交。
