# OpenZLTravel

[English](README.md) | **简体中文**

![Python](https://img.shields.io/badge/Python-3.11--3.13-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)
![LangChain](https://img.shields.io/badge/LangChain-1.x-1C3C3C)
![LangGraph](https://img.shields.io/badge/LangGraph-1.x-FF6B35)
![Vue](https://img.shields.io/badge/Vue-3-42B883?logo=vuedotjs&logoColor=white)

> [!IMPORTANT]
>
> OpenZLTravel 是一个用于学习 LangChain 与 LangGraph 工程边界的开发态 AI 旅行规划工作台。它不是预订平台，不提供支付、下单、机票查询、多城市行程或正式账号系统，查询结果也不构成库存或价格承诺。

用户通过自然语言说明出发地、目的地、日期、人数、预算与偏好。Assistant 查询并校验景点、车次、酒店和天气；用户确认这些事实后，服务端签发短时旅行工单，再由 TravelGraph 生成日程、路线与预算供用户确认。确认后的行程只保存到当前运行实例的历史记录。

![OpenZLTravel 工作台](docs/images/openzltravel-assistant.png)

## 使用流程

1. 在对话中补齐旅行需求。
2. 从服务端提供的卡片中选择城市、景点、去返程车次与酒店；席别偏好可在对话中说明，交通或住宿也可以标记为自行安排。
3. Assistant 刷新时间敏感事实并签发 `TravelOrder Token`。
4. TravelGraph 生成逐日安排、相邻行程点之间的路线和预算。
5. 用户确认方案，或提交一种受支持的修改。
6. 最终行程进入当前运行实例的历史抽屉，可查看或删除。

## 架构与信任边界

- LangChain Assistant 负责自然语言交流和只读事实工具；TravelGraph 不读取聊天记录，也不调用 LLM。
- 城市、POI、车次和酒店都使用服务端校验过的事实 ID；前端不能自行构造事实或价格。
- Graph 初始输入只接受 `{"order_token":"..."}`，随后按固定路径执行规划。
- 上游没有返回的票价、房价、天气或路线成本保持未知，并生成明确提示。
- `route_preview` 是唯一中断点，只接受两种修改：“把某景点放到第 N 天”或“第 N 天少安排一个”；后一种修改仍会把所有已选景点保留在行程中。

```mermaid
flowchart LR
    U[用户] --> UI[Vue 工作台]
    UI --> A[LangChain Assistant]
    A <--> P[地点目录 / 高德 / 12306 / RollingGo / 天气]
    A -->|签名 TravelOrder Token| V[validate_order]
    V --> I[build_itinerary]
    I --> R[build_routes]
    R --> B[calculate_budget]
    B --> C[validate_plan]
    C --> Q{route_preview}
    Q -->|受支持的修改| I
    Q -->|确认| S[save_trip]
```

| 模块 | 职责 |
| --- | --- |
| `backend/src/assistant/` | 对话、事实工具、选择校验、会话快照与工单交接 |
| `backend/src/travel_graph/` | 工单验证、确定性规划、路线、预算、中断与保存 |
| `backend/src/domain/` | 领域模型、规划算法与事实边界校验 |
| `backend/src/infrastructure/` | PostGIS、高德、12306、RollingGo 与天气 Provider |
| `backend/src/runtime/` | 配置、依赖装配、匿名身份与签名 Token |
| `frontend/src/features/` | 对话、事实卡片、规划确认、结果与历史界面 |

详细设计见 [ARCHITECTURE.md](ARCHITECTURE.md)，完整调用顺序见 [docs/CODE_FLOW.md](docs/CODE_FLOW.md)。

## 快速开始

最短路径使用内置 `fake` Provider。其事实固定为杭州演示数据，因此不需要下载地点库，也不需要高德、RollingGo 或 12306 凭证；Assistant 仍然需要一个已配置且可访问的 OpenAI 兼容对话模型，并且端点必须支持工具调用和 `response_format=json_object`。

Docker 启动路径要求宿主机安装 Git、Windows PowerShell 和 Docker Desktop，并且能够访问网络。Python 与 Node.js 已包含在镜像中，不是这条路径的宿主机前置条件。如果已经下载源码，请从项目根目录开始并跳过 `git clone`。

### 1. 创建本机配置

```powershell
git clone https://github.com/Kkkirito-123/OpenZLTravel.git
cd OpenZLTravel
Copy-Item backend/.env.example backend/.env
Copy-Item backend/.env.catalog.example backend/.env.catalog.local
```

在 `backend/.env` 中设置以下值。当前 Compose 文件读取 `LLM_*` 变量，再将它们映射成应用使用的 `OPENAI_*` 变量，因此只填写原模板中的 `OPENAI_*` 项不足以启动 Compose。

```dotenv
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://your-openai-compatible-endpoint/v1
LLM_MODEL=your-model
LLM_TIMEOUT_SECONDS=60

PROVIDER_MODE=fake
RAIL_PROVIDER=public
AUTH_SECRET=change-this-local-secret-to-at-least-32-characters
```

在 `backend/.env.catalog.local` 中设置两个不同的本机密码。建议使用足够长的字母数字组合，确保它们在 URI 和 libpq 连接串中均可直接使用；其他特殊字符需要按对应格式转义。即使使用 `fake` 模式，统一 Compose 仍会启动 PostGIS，因此两个密码都不能留空；`CATALOG_DATABASE_URL` 则是 `live` 模式地点目录管理脚本的额外要求。

```dotenv
CATALOG_POSTGRES_PASSWORD=replace-with-owner-password
CATALOG_DATABASE_URL=postgresql://catalogowner:replace-with-owner-password@127.0.0.1:55432/openzltravelcatalog
TRAVELAPP_POSTGRES_PASSWORD=replace-with-reader-password
```

这两个本机文件已被 `.gitignore` 排除，不要提交真实凭证。

### 2. 校验并启动

```powershell
docker compose --env-file backend/.env --env-file backend/.env.catalog.local config --quiet
.\start.ps1 up
.\start.ps1 ps
```

如果 Windows 执行策略阻止本机脚本，可在不修改整机策略的情况下执行同一操作：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 up
```

打开 [http://127.0.0.1:5173](http://127.0.0.1:5173)。首次构建需要下载 Python、Node.js 基础镜像和项目依赖，耗时取决于网络情况。

| 服务 | 地址 | 职责 |
| --- | --- | --- |
| `frontend` | `127.0.0.1:5173` | Vue 工作台与同源代理 |
| `assistant` | `127.0.0.1:2030` | LangChain 对话、工具和工单签发 |
| `agent` | `127.0.0.1:2024` | LangGraph Thread、Run 与行程 API |
| `catalogdb` | `127.0.0.1:55432` | PostGIS 地点目录；`fake` 模式不会查询它 |

常用管理命令：

```powershell
.\start.ps1 logs
.\start.ps1 restart
.\start.ps1 down
```

`down` 会保留命名卷 `openzltravelcatalogdata`。除非确实要删除已恢复的地点库，否则不要运行 `docker compose down -v`。

## 真实数据

使用 `PROVIDER_MODE=live` 时，Assistant 会查询真实地点目录和外部 Provider。恢复流程会调用 `catalog.ps1`，因此先在宿主机安装 Python 3.11 至 3.13 以及后端包：

```powershell
cd backend
python -m pip install -e .
cd ..
```

当前 `catalog.ps1` 只把地点目录 env 文件交给 Compose，却会解析整份 Compose 配置。在脚本同步传入 `backend/.env` 之前，请用下面的隔离调用替换恢复说明最后两条 `catalog.ps1` 命令。模型占位变量只存在于子进程中，执行策略覆盖也仅对该进程生效，并且只会启动 `catalogdb`。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command {
    $env:LLM_API_KEY = "catalog-only-placeholder"
    $env:LLM_BASE_URL = "http://catalog-only.invalid/v1"
    $env:LLM_MODEL = "catalog-only-placeholder"
    .\catalog.ps1 -ProvisionRuntime
    .\catalog.ps1 -Verify
}
```

然后准备 PostGIS 地点目录：

1. 下载 [2026-08-28 地点目录备份](https://drive.google.com/file/d/1bfyU_XFjQcnFIaAehMtYXhZZdvu1RdXQ/view?usp=drive_link)。
2. 按 [恢复说明](docs/data/RESTORE.md) 校验 SHA256、导入 `catalog` Schema，并创建只读 `travelapp` 账号。
3. 在 `backend/.env` 中设置 `PROVIDER_MODE=live`，按需加入高德与 RollingGo 凭证，再重启整套服务。

备份中的 dump 为 346.67 MiB，恢复后的 Schema 约 2.54 GiB，包含 135,533 条 POI。[数据清单](docs/data/catalog-20260828.json) 记录了数据量、来源、哈希和许可证。恢复流程会替换目标数据库已有的 `catalog` Schema，请先确认目标库。

| 数据源 | 默认行为 | 凭证 |
| --- | --- | --- |
| PostGIS Catalog | `live` 模式的主要地点目录 | 必需，来自 `backend/.env.catalog.local` |
| 高德开放平台 | 地点兜底、公交或实时驾车及天气兜底；不可用时降级 | 可选，`AMAP_API_KEY` |
| 12306 | 默认调用公共查询接口 | `RAIL_PROVIDER=public` 时无需 Key |
| RollingGo | 搜索酒店；失败时降级为地点目录中的酒店 | 可选，`ROLLINGGO_API_KEY` |
| Open-Meteo | 提供天气预报 | 无需 Key |

## 当前限制

- 只支持单目的地、1 至 7 天行程。
- Assistant 快照保存在当前浏览器的 `sessionStorage`，服务端没有对话数据库。默认会话 Token 有效 12 小时，旅行工单 Token 有效 10 分钟。
- Checkpoint 使用 `InMemorySaver`，因此进程重启后会丢失执行中的 Graph Checkpoint。`start.ps1 restart` 会强制重建 Agent 容器；由于没有挂载运行数据卷，Thread、Run 和历史行程 Store 也会被清空。PostGIS 命名卷不受此限制。
- 预算是规划估算。餐饮和门票使用固定规则，未知票价、房价与市内交通不会被补造，因此总额可能低估。
- 12306 通常只覆盖预售窗口，天气通常只覆盖未来 16 天；远期日期或上游失败时会明确显示未知或降级状态。
- 标准 `auto`、`walk` 和 `driving` 路线使用本地距离估算；只有 `transit` 和 `realtime_driving` 会尝试高德，并在高德不可用时降级为本地估算。

## 开发与验证

本地开发使用 Python 3.11 至 3.13，建议使用 Node.js 22。先安装依赖：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd ..\frontend
npm.cmd ci

cd ..
```

执行静态检查、测试和前端构建：

```powershell
cd backend
.\.venv\Scripts\python.exe -m ruff check src tests catalog_builder
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest -q

cd ..\frontend
npm.cmd test
npm.cmd run build

cd ..
docker compose --env-file backend/.env --env-file backend/.env.catalog.local config --quiet
```

默认测试使用 Fake Provider。只有 `CATALOG_TEST_DATABASE_URL` 指向单独的 `openzltravelcatalogtest` 数据库时，才会运行真实 PostGIS 集成测试；只有执行 `python -m pip install -e ".[dev,catalog]"` 安装可选地点目录依赖后，才会运行 pyosmium 数据源测试。通过上述命令不代表外部 Provider 已经完成验证。

## 项目结构

```text
OpenZLTravel/
|-- backend/
|   |-- src/assistant/          # LangChain Assistant 服务
|   |-- src/travel_graph/       # LangGraph 规划图与 API
|   |-- src/domain/             # 领域模型和确定性规划
|   |-- src/infrastructure/     # 地点目录与外部 Provider
|   |-- src/runtime/            # 配置、身份、Token 和依赖装配
|   |-- catalog_builder/        # 地点目录构建、权限与验证
|   |-- benchmarks/             # 固定功能与质量评测
|   `-- tests/
|-- frontend/src/features/      # Assistant、Planning 与 Trips 界面
|-- docs/                       # 调用流程、数据清单与恢复说明
|-- README.md                   # 英文指南
|-- README.zh-CN.md             # 简体中文译文
|-- compose.yml
|-- langgraph.json
|-- catalog.ps1
`-- start.ps1
```

## 许可与署名

仓库目前没有根级 `LICENSE` 文件，因此项目代码尚未声明开源许可证。地点目录聚合了 OpenStreetMap、GeoNames、Modood、AreaCity 等公开数据；使用或再分发备份时，请保留 [数据清单](docs/data/catalog-20260828.json) 中的来源与许可证信息。

## 灵感来源

- [tutu-zzz/zhilv-yuntu](https://github.com/tutu-zzz/zhilv-yuntu)
- [Reyzowter/Hello-Agents](https://github.com/Reyzowter/Hello-Agents)
