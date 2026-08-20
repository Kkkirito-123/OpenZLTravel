# OpenZLTravel LangGraph Platform

OpenZLTravel 是一个基于 LangGraph Platform 的单目的地旅行规划工作台。整个业务由一个
`travel` 根图、一套 `TravelState`、三个职责固定的 LLM Agent 和一个 Vue 工作台组成；
Thread/Checkpoint 管理短期执行状态，Store 只保存最终行程和用户明确授权的稳定偏好。

## 当前架构

```text
Vue Workbench
    │ LangGraph JS SDK：Thread / Run / Stream / interrupt-resume
    ▼
LangGraph Agent Server（唯一图：travel）
    ├─ RequirementAgent：只补充结构化需求
    ├─ PlannerAgent：只引用 Provider 事实 ID 生成草稿
    ├─ ReviewAgent：只读审查，最多触发一次修订
    ├─ 确定性节点：推荐、路线、预算、校验、保存
    ├─ Checkpoint：Thread 的短期执行状态
    └─ Store：(user_id, trips) / (user_id, preferences)
             │
             ├─ PostGIS Catalog（本地目录）
             ├─ 高德（目录与路线兜底）
             ├─ Open-Meteo（天气）
             ├─ 12306 公共接口（默认）/ MCP（可选）
             └─ RollingGo（酒店）
```

设计细节见 [ARCHITECTURE.md](ARCHITECTURE.md)，开发约束见 [AGENTS.md](AGENTS.md)。
如果你是第一次学习 LangGraph，建议先阅读 [docs/LEARNING_GUIDE.md](docs/LEARNING_GUIDE.md)，
再按指南中的顺序打开代码；它会把 State、Node、Edge、Reducer、interrupt 和 Checkpoint
分别对应到本项目的真实文件。

## 核心能力

- 固定阶段：`collecting → discovering → awaiting_selection → planning → reviewing → completed`。
- 需求不完整时使用 `clarification` interrupt 追问，可多次恢复。
- 未指定具体城市时，Catalog 按数据覆盖和偏好确定性推荐最多 5 个真实城市。
- Catalog 就绪后并行查询铁路、酒店和天气，单个 Provider 失败会给出明确降级选项。
- PlannerAgent 只能引用地点、酒店和车次的稳定 ID，不能创建名称、坐标、价格或路线。
- PlannerAgent 超时后回退确定性规划；ReviewAgent 超时后仍继续确定性最终校验。
- ReviewAgent 最多让 PlannerAgent 修订一次，不存在无限反思循环。
- 最终校验通过后，以 `user_id + thread_id` 派生稳定 `trip_id`，重复 Run 不会重复保存。
- 只有用户明确说“记住”或“忘记”时，才修改长期稳定偏好。
- Docker 端口只绑定 loopback，前端通过签名 Cookie 访问 Agent，所有资源继续按 owner 隔离。

## 目录

```text
Docker Compose
├─ catalogdb                  PostGIS 地点目录
├─ agent                      LangGraph Agent Server
└─ frontend                   Vue / Vite 旅行工作台

langgraph.json                 Agent Server 配置，只导出 travel
compose.yml                    唯一运行时编排文件
start.ps1                      Docker 启动、停止、日志入口
backend/
  Dockerfile                   Agent Server 镜像
  src/                         直接按职责放置后端代码，不再套项目同名包
    travel_graph/              LangGraph：State、Node、Edge、Agent、interrupt
      nodes/                   按需求、发现、规划、审查、保存拆分节点
    domain/                    纯模型、解析、规划和最终校验
    providers/                 外部事实适配器与 Fake Provider
    catalog/                   PostGIS 查询、Windows 兼容层与目的地评分
      tool.py                 CatalogTool 与数据库仓储
      ranking.py              纯确定性目的地评分
      postgres_compat.py      Windows Proactor 下的线程同步查询适配
    api/                       Agent Server Auth、FastAPI 和行程历史
    runtime/                   配置、依赖装配、Protocol 和模型网关
  catalog_builder/             独立地点目录构建子系统
  tests/                       新架构与 Catalog 测试
frontend/
  Dockerfile                   Vue / Vite 工作台镜像
  src/pages/WorkbenchPage.vue  唯一页面
  src/composables/             唯一 Thread 状态入口
  src/services/                LangGraph SDK 与历史 HTTP 网关
  src/components/              对话、进度、选择、行程和历史抽屉
```

### 为什么改成 `backend/src`？

`src` 是 Python 项目常见的“源码根目录”。本项目直接在它下面按职责分包，不再增加项目名
这一层，导入也只表达代码职责，例如：

```python
from domain.models import TravelRequirements
from travel_graph.state import TravelState
```

可以把它理解成下面的关系：

```text
openzltravel/                  ← 项目仓库（前端 + 后端 + 文档）
└── backend/
    └── src/                   ← Python 源码根目录
        ├── travel_graph/      ← LangGraph 工作流
        ├── domain/           ← 业务规则
        └── providers/ ...    ← 其他职责包
```

本地图目录使用 `travel_graph`，不直接使用 `langgraph`：否则它会遮蔽官方依赖包
`langgraph`，使 `from langgraph.graph import StateGraph` 导入到项目自身。`langgraph.json`
从 `./backend/src/travel_graph/application.py:travel` 加载唯一根图。

### 后端导入规范

- 导入中禁止出现 `src` 或 `openzltravel` 前缀，只使用职责包名。
- 跨职责使用绝对导入，例如 `from domain.models import City`。
- 同一职责包内部可以使用相对导入，例如 `providers` 内部的 `from .base import ...`。
- `domain` 不依赖 LangGraph、HTTP 或 Provider；`travel_graph` 通过 `runtime.contracts` 使用外部能力。
- `api` 不参与图编排；只有 `travel_graph/nodes/persistence.py` 可以保存最终行程。

## Docker 统一启动

运行时只依赖 Docker Desktop，不再要求宿主机单独启动 Python、LangGraph CLI
或 Node.js 进程。首次使用时准备两个被 Git 忽略的本机配置文件：

```powershell
Copy-Item backend/.env.example backend/.env
Copy-Item backend/.env.catalog.example backend/.env.catalog.local
.\start.ps1
```

当前开发机已有这两个文件时，直接执行 `.\start.ps1` 即可，不要覆盖现有
Key 或数据库密码。脚本会构建并启动：

- `catalogdb`：复用 `openzltravelcatalogdata` 数据卷。
- `agent`：从 `backend/.env` 注入模型、高德和 RollingGo 配置。
- `frontend`：通过 Docker 内网代理到 `agent:2024`。

浏览器只需打开 <http://127.0.0.1:5173>；Agent 健康端点为
<http://127.0.0.1:2024/ok>。两个端口都只绑定宿主机 loopback。

常用命令：

```powershell
.\start.ps1             # 构建并启动，等待三个服务就绪
.\start.ps1 ps          # 查看容器状态
.\start.ps1 logs        # 跟踪容器日志
.\start.ps1 restart     # 重新构建并重启
.\start.ps1 down        # 停止容器，保留 Catalog 数据卷
```

`down` 故意不带 `--volumes`。不要执行 `docker compose down -v`，否则会删除
已构建的地点目录数据卷。

### Fake 与 Live Provider

`backend/.env` 是 Agent 的唯一 API Key 入口。设置 `PROVIDER_MODE=fake` 可以离线学习
完整图流程；设置 `PROVIDER_MODE=live` 则使用 Catalog、12306、RollingGo 和天气
等真实 Provider。修改 `.env` 后执行 `.\start.ps1 restart`。

## Live Provider

如果是全新的 Catalog 数据卷，先准备独立 PostGIS 地点目录：

```powershell
Copy-Item backend/.env.catalog.example backend/.env.catalog.local
.\start.ps1
.\catalog.ps1 -Build
.\catalog.ps1 -ProvisionRuntime
```

`catalog_builder` 默认读取 `backend/data/raw/` 下的 GeoNames 与 OSM 原始数据。
`catalog.ps1 -ProvisionRuntime` 会幂等创建只读 `travelapp`；Compose 会使用
`backend/.env.catalog.local` 自动组成容器内连接，不需要再把宿主机地址写入
`backend/.env`。启用真实 Provider 只需设置：

```dotenv
PROVIDER_MODE=live
```

其余可选配置见 [backend/.env.example](backend/.env.example)：

- `AMAP_API_KEY`：Catalog 未命中与路线查询兜底。
- `RAIL_PROVIDER=public`：直接使用 ZLAgent 同款的 12306 公共查询接口；只有接入外部铁路 MCP 时才改为 `mcp`，并配置 `RAIL_MCP_URL` / `RAIL_MCP_TOKEN`。
- `ROLLINGGO_MCP_URL` / `ROLLINGGO_API_KEY`：RollingGo 官方酒店 Streamable HTTP MCP。Key 只放在 `backend/.env`，不要提交到 Git。
- `OPENAI_API_KEY`：启用三个 Agent；未配置时需求追问、规划和审查都按规则降级。

Provider 运行时只使用进程内异步 TTL 缓存、同请求合并、每 Provider semaphore、明确超时
和最多一次网络重试。分布式 Provider 缓存与多 Worker 部署不在当前版本范围内。

## 图公开契约

新消息输入固定为：

```json
{"messages":[{"role":"user","content":"上海出发，10 月 2 日到杭州玩三天"}]}
```

interrupt 使用 `kind` 区分类型：

```json
{"kind":"clarification","question":"请补充：开始日期。","missing_fields":["start_date"],"error":null}
```

```json
{"kind":"destination_selection","candidates":[],"error":null}
```

```json
{"kind":"travel_selection","outbound_options":[],"return_options":[],"hotel_options":[],"requires_hotel":true,"self_arranged_allowed":true,"error":null}
```

恢复统一通过 `Command(resume=...)`，载荷必须与当前 interrupt 类型一致：

```json
{"kind":"clarification","values":{"origin":"上海"}}
```

```json
{"kind":"destination_selection","candidate_id":"destination:..."}
```

```json
{"kind":"travel_selection","selection":{"self_arranged_outbound":true,"self_arranged_return":true,"self_arranged_hotel":true}}
```

错误类型不会推进状态，而会返回同类型 interrupt，并在 `error` 中提供稳定错误码和消息。

## 自定义 HTTP 接口

只保留四个业务接口：

```text
POST   /api/auth/anonymous
GET    /api/trips
GET    /api/trips/{trip_id}
DELETE /api/trips/{trip_id}
```

Thread、Run、Checkpoint 和 Store 统一使用 LangGraph Agent Server 的标准接口，自定义
FastAPI 应用仅承担匿名身份签发与最终行程历史访问。

## 验证

```powershell
cd backend
python -m ruff check src tests catalog_builder
python -m mypy
python -m pytest -q

cd ../frontend
npm.cmd test
npm.cmd run build
```

最后应从仓库根目录执行 `.\start.ps1`，确认三个容器就绪，再完成一次
新建 Thread、流式 Run、`travel_selection` 恢复、`completed` 和历史读取冒烟。

## 当前范围

首版聚焦单目的地规划、候选选择、流式恢复和历史行程，不包含逐日编辑、Markdown 导出、
支付、下单、多城市规划或生产多 Worker 部署。

本仓库中的 `.env`、`db/`、Provider Token、离线原始数据和构建产物均为本机数据，不应提交。
