# OpenZLTravel V0.5

OpenZLTravel 是一个支持匿名访客隔离的多轮旅行助手。用户先通过对话补全目的地、日期和预算，
需求完整后进入原有旅行工作台，并行查询车票、酒店、天气和本地 POI。项目以 editable
方式复用 OpenZLAgent 的模型客户端、上下文清单、会话保存和滚动摘要，但不修改其核心代码。
V0.5 增加显式长期偏好、静态 Skill 契约、会话 Token 账本和两级意图缓存。

## 核心流程

```text
用户消息
  ↓ 快速解析 → 精确意图缓存 → 受限 LLM Command Generator
Skill 注册表选择确定性 Flow
  ↓
旅行状态合并与确定性 Flow
  ↓ 信息不足则继续追问
创建规划会话
  ↓ 立即返回 session_id
并行发现：12306 车票 / RollingGo 酒店 / Open-Meteo 天气 / 本地 OSM POI
  ↓ 各步骤独立展示进度、缓存命中与降级状态
选择去程、返程、席别和酒店
  ↓
确定性分天、路线与预算计算
  ↓ 可选 LLM 文案润色，最多等待 8 秒
校验完整结构
  ↓
PostgreSQL 保存共享业务状态和完整行程
  ↓
逐日编辑、局部路线与预算重算、Markdown 导出
```

当前能力：

- 支持可刷新恢复的多轮对话；聊天模型只能设置受控旅行槽位，不能直接调用工具。
- 当前消息、权威状态、待回答字段、Skill 契约、长期偏好、最近完整轮次和滚动摘要组成专属上下文。
- 用户明确说“记住/忘记”时，保存或删除跨会话稳定偏好；当前明确输入始终优先。
- 意图模型不设置生成 Token 或会话累计 Token 硬上限，前端只记录成功调用的实际或估算用量。
- 相同意图提示先读 PostgreSQL 精确结果缓存；同一进程的并发请求只产生一次模型调用。
- “省域推荐”和“周边推荐”先收集结构化需求，不生成缺少数据支持的城市结论。
- 具体城市、日期和预算完整后，以消息版本作为幂等键创建一次现有规划会话。
- 创建 1～7 天、国内单目的地的持久规划会话，页面轮询独立步骤状态。
- 去程与返程并行查询 12306 直达车、余票和票价；无直达或用户主动展开时查询中转。
- 中转方案被选中后才补查两段票价，避免首次发现放大请求量。
- RollingGo 提供酒店价格、图片、设施和房型；未登录或失败时回退本地 OSM 酒店。
- Open-Meteo 提供天气，高德仅作天气兜底；日期不覆盖时明确标记“暂无预报”。
- 本地 GeoNames / OSM 提供城市和 POI，未覆盖时可按配置回退高德。
- 普通步行和驾车使用本地估算；公交、地铁和实时驾车才调用高德。
- 确定性规划器根据车次时刻、节奏、地点距离和酒店位置分天，首末日不会强塞景点。
- LLM 只能润色摘要、主题和提示，不能修改地点、车票、酒店、天气、路线或价格。
- 结果页支持地点详情、真实路线地图、每日预算、拖拽/按钮排序和单日局部重算。
- 会话支持幂等创建、取消、重试和重启恢复；失败时不保存半成品。
- 浏览器通过 HttpOnly 匿名 Cookie 获得访客身份；所有对话、规划和行程查询都同时校验访客 ID。
- PostgreSQL 唯一约束提供跨进程幂等保障；Redis 多 Worker 协调属于后续 PR，不在当前运行链路中。

## 架构

后端按职责而不是技术层级拆分，对话内核与供应商代码保持隔离：

```text
backend/app/
  main.py               FastAPI 路由、异常映射、依赖装配、就绪检查
  assistant.py          助手会话、消息幂等、上下文记录和规划会话衔接
  dialogue.py           对话内核兼容导出层
  dialogue_commands.py  受限 Command、槽位名称和纯状态类型
  dialogue_context.py   专属上下文清单与最近完整轮次渲染
  dialogue_generator.py 受限 LLM 调用、用量记录和请求合并
  dialogue_flow.py      快速解析、槽位合并和确定性追问
  skills.py             两个静态 Skill 的触发条件、任务输入与允许副作用
  config.py             环境变量配置
  models.py             Pydantic 请求、事实、会话和行程模型
  errors.py             稳定错误码
  runtime.py            后台任务、幂等、恢复、重试、取消和步骤状态
  workflow.py           编译并复用 LangGraph 发现图与生成图
  travel.py             行程组装、候选校验、编辑重算和行程读写
  travel_budget.py      经验预算、真实车票/酒店报价合并与超额提示
  travel_export.py      已校验行程的 Markdown 导出
  catalog.py            PostgreSQL 地点、行政区和附近 POI 查询
  storage.py            PostgreSQL 业务 Repository、事务、幂等和 revision 更新
  identity.py           匿名 Cookie、Token 哈希和旧数据认领
  database/app.sql      PostgreSQL app Schema 与中文 COMMENT
  scripts/              一次性 SQLite 只读迁移工具
  providers/
    base.py             MCP 生命周期、缓存执行器、去重、并发、重试和熔断
    maps.py             本地优先、调度和交通降级策略，兼容既有导入
    amap.py             高德 HTTP、缓存、限流与响应解析
    weather.py          Open-Meteo 预报与日期覆盖判断
    geo.py              WGS-84/GCJ-02、POI 解析和本地路线估算
    rail.py             12306 MCP 解析、票价合并和中转补价
    hotels.py           RollingGo OAuth、兼容 DIDA 和 OSM 酒店降级
    planner.py          确定性规划、受控旧规划器和可选文案润色
```

```text
frontend/src/
  main.ts / App.vue     路由与应用壳
  api.ts / types.ts     稳定 API 和类型
  TripMap.vue           只绘制供应商返回的真实轨迹
  styles/               基础、工作台、行程、历史、对话和响应式样式
  pages/
    ChatPage.vue        多轮需求收集、恢复和当前需求摘要
    PlanPage.vue        独立表单规划入口
    SessionPage.vue     第二步：进度、车票和酒店选择
    TripPage.vue        第三步：行程、地图、预算和编辑
    HistoryPage.vue     已保存行程
```

依赖方向：

```text
Vue → FastAPI main → TravelAssistantService → Dialogue Flow
                                      ├─ OpenZLAgent Conversation / Context
                                      └─ PlanningRuntime → WorkbenchWorkflow
                                                            ├─ providers
                                                            ├─ TravelService
                                                             ├─ PostgreSQL app
                                                             └─ PostgreSQL catalog
```

LangGraph 只表达节点依赖，不承担事实判断、持久化或 Agent 自主循环。数据发现图在本地
POI 准备完成后并行执行去程、返程、酒店和天气；生成图依次执行确定性规划、可选文案
润色、交通查询、组装与最终校验。

完整认知层设计见 [ASSISTANT_ARCHITECTURE.md](ASSISTANT_ARCHITECTURE.md)。
本地运行、故障处理和验证流程见 [OPERATIONS.md](OPERATIONS.md)。

## 对话上下文边界

- `TravelDialogueState` 是任务事实，按消息版本保存；滚动摘要只帮助理解，不能覆盖槽位。
- 默认最多读取 5,000 字符上下文，意图生成不设置输入、输出或会话累计 Token 硬限制；
  单次超时 15 秒，并记录供应商实际用量或本地保守估算。
- 每轮只允许 `StartFlow / SetSlot / ClearSlot / Confirm / CancelFlow / RouteToChat /
  RememberSlot / ForgetMemory`；记忆命令还必须通过当前消息中的显式授权校验。
- 意图阶段只加载 Skill 的无正文契约，不加载 Skill 实现、POI、车票、酒店、地图数据或
  供应商参数。
- 明确天数、预算、人数、日期、确认和取消优先由快速解析处理；存在剩余语义才调用模型。
- `message_id` 保证双击和重试幂等；状态与响应原子保存，模型失败时不推进版本。
- PostgreSQL `app.dialoguesession` 保存权威状态，`app.dialoguerequest` 保存幂等响应；
  `app.travelmemory` 保存显式长期偏好；OpenZLAgent 固定的 `app.session_turns` 与
  `app.session_summaries` 保存完整轮次和滚动摘要。
- 浏览器 Cookie 只保存随机 Token，数据库只保存哈希；访客资源查询必须同时匹配 `visitorid`
  和资源 ID，资源不属于当前访客时统一返回 404。

## 数据边界

- 模型永远不能创建或修改 POI、车次、酒店、天气、路线和价格事实。
- 地图只绘制响应中的 `polyline`；本地估算没有轨迹时只显示标记，不画两点直线。
- 领域坐标统一为 WGS-84，在高德 API 和高德 JS 地图边界转换为 GCJ-02。
- 未知票价和房价保持空值，从预算排除并增加警告，不能使用经验值冒充实时报价。
- 预算属于透明估算：本地交通、餐饮、门票和其他费用按固定规则计算，选定车票和酒店
  使用供应商报价；用户预算仅用于超额提醒。
- 第三方图片只保存合法 HTTP(S) URL，不下载、代理或放入模型提示。
- 最终结构通过校验后才写入 `trips`；发现和生成中的状态写入 `planning_sessions`。

## 外部服务配置

后端从 `backend/.env` 读取配置，前端地图 Key 从 `frontend/.env` 读取。两个文件均被 Git
忽略，禁止提交真实密钥。

```powershell
Copy-Item backend/.env.example backend/.env
Copy-Item frontend/.env.example frontend/.env
```

最小可运行配置：

- 本地目录已构建时，城市、POI 和普通交通不需要高德 Web 服务 Key。
- Open-Meteo 不需要 Key。
- `RAIL_MCP_URL` 默认是 `http://127.0.0.1:8001/mcp`。
- RollingGo 未登录且未配置旧 `DIDA_API_KEY` 时自动展示本地 OSM 酒店。
- LLM 未配置时工作台仍可使用确定性中文模板；聊天首页会返回 `intent_not_configured`，
  可改用 `/plan` 表单入口。
- `VITE_AMAP_JS_KEY` 只用于前端地图；缺失时结果页显示配置提示。

### 12306 MCP

12306 MCP 作为独立只读服务运行。安装后在单独终端启动 8001 端口：

```powershell
python -m pip install mcp-server-12306
$env:SERVER_HOST="127.0.0.1"
$env:SERVER_PORT="8001"
mcp-12306
```

后端实现标准 MCP Streamable HTTP 初始化、`initialized` 通知、协议版本和
`Mcp-Session-Id` 生命周期；会话过期时只重建一次。

车票查询会先通过 MCP 的车站搜索能力把城市或车站名称解析为 12306 三字码，再查询
余票和票价。12306 通常只开放未来约 14 天的车票，超出预售范围时页面会显示明确的
降级提示，不会把空结果误显示成服务正常但没有车次。

> [mcp-server-12306](https://github.com/drfccv/mcp-server-12306) 是非官方数据接口，
> 本项目仅用于学习、研究和只读查询，不提供登录、验证码、抢票、支付或自动下单。
> 使用时必须遵守其免责声明、12306 规则和适用法律，不得用于商业滥用。

### RollingGo 酒店 Skill

项目通过 [rollinggo-hotel-booking](https://github.com/RollingGo-AI/rollinggo-hotel-skill-CN)
提供的 OAuth 登录复用酒店服务，不需要把访问令牌写入项目 `.env`：

```powershell
npm.cmd install -g @rollinggo/hotel@latest
rgh.cmd login
```

登录令牌默认保存在用户目录的 `.hotel-cli/token.json`；后端只在请求时读取，不写入
PostgreSQL、日志或 API 响应。酒店详情仅在用户打开抽屉时加载。未登录、认证失败、超时或
熔断时，住宿步骤回退本地 OSM 候选，不阻断行程。旧的 `DIDA_API_KEY` 配置仍兼容。

当前工作台只接入搜索与详情。锁价、下单和支付属于真实消费操作，必须另行设计用户
二次确认和入住人信息保护，本版本不会自动执行。

## 启动

安装并启动后端：

```powershell
cd backend
python -m pip install -e ..\..\..
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

另开终端启动前端：

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

访问 <http://127.0.0.1:5173>。也可在项目根目录执行 `./start.ps1 -Install`；脚本使用
`npm.cmd`，不会触发 PowerShell 对 `npm.ps1` 的执行策略限制。12306 MCP 仍需单独启动。

## API

```text
POST   /api/assistant-sessions
GET    /api/assistant-sessions/{id}
POST   /api/assistant-sessions/{id}/messages
GET    /api/assistant-skills
POST   /api/visitor/claim
GET    /api/assistant-memories
DELETE /api/assistant-memories/{key}

POST   /api/planning-sessions
GET    /api/planning-sessions/{id}
PUT    /api/planning-sessions/{id}/selection
POST   /api/planning-sessions/{id}/rail/transfers
GET    /api/planning-sessions/{id}/hotels/{hotel_id}
POST   /api/planning-sessions/{id}/generate
POST   /api/planning-sessions/{id}/retry
DELETE /api/planning-sessions/{id}

GET    /api/trips
GET    /api/trips/{id}
DELETE /api/trips/{id}
GET    /api/trips/{id}/export/markdown
GET    /api/trips/{id}/alternatives
PATCH  /api/trips/{id}/days/{day_index}

GET    /health
GET    /ready
```

旧 `POST /api/trips` 保留为兼容快速入口，网页不再使用。创建会话建议携带
`Idempotency-Key`，相同键会返回原会话。

除 `/health`、`/ready` 和 `/api/assistant-skills` 外，接口都需要浏览器的
`openzltravelvisitor` HttpOnly Cookie。Cookie 只保存随机 Token，后端写入 SHA-256 哈希；
迁移旧 SQLite 后得到的认领码通过 `POST /api/visitor/claim` 使用一次，24 小时后失效。

## 缓存、恢复与降级

`app.providercache` 与业务会话保存在 PostgreSQL 中，连接池默认 2～20。缓存键不包含 API Key：

| 数据 | TTL |
|---|---:|
| 12306 直达、中转、票价 | 2 分钟 |
| RollingGo / DIDA 酒店搜索 | 10 分钟 |
| RollingGo / DIDA 酒店详情 | 5 分钟 |
| Open-Meteo / 高德天气 | 30 分钟 |
| 高德公交 / 地铁 | 30 分钟 |
| 高德实时驾车 | 5 分钟 |
| 高德城市 / POI 兜底 | 24 小时 |
| 意图命令精确结果 | 1 小时 |

同一进程的相同请求共享任务；每个外部 Provider 独立限并发、最多一次网络重试和熔断。
认证失败、限流和业务错误不重试。服务重启后恢复 `searching` / `generating` 会话；如果
完整行程已经保存，恢复任务直接标记完成，不重复写入。

当前 PR 2 使用 PostgreSQL 处理共享状态、事务和跨进程唯一约束，仍保持单 Uvicorn Worker。
Redis 协调、分布式锁、任务租约和多 Worker 属于后续 PR 3；模型端 KV Cache 不由应用持有，
仅在供应商明确支持时设置 `INTENT_PROMPT_CACHE_KEY`，并通过返回的 cached token 指标验证是否真正命中。

降级规则：12306 失败可自行安排，RollingGo 失败回退 OSM，天气失败标记未知，高德路线失败
使用本地估算。地点未命中允许高德兜底；PostgreSQL 连接故障直接返回
`catalog_unavailable`，避免故障期间集中消耗高德配额。

## 本地公开数据

运行时默认读取 `openzltravelcatalog.catalog`，使用 PostGIS 查询城市 80 公里内景点、餐厅
和酒店。应用使用只读 `travelapp` 账号，数据库所有者只用于构建。首次配置运行账号：

```powershell
.\catalog.ps1 -Start
.\catalog.ps1 -Runtime
```

`-Runtime` 会生成被 Git 忽略的 `backend/.env.runtime.local`，不会改写已有 `.env`。正常
运行不再读取 `backend/data/catalog.sqlite3`。旧 SQLite 只由一次性迁移脚本只读打开，
迁移完成后通过认领码转移到当前匿名访客。

完整初始化、迁移和认领步骤见 [OPERATIONS.md](OPERATIONS.md)。

原始 OpenStreetMap、GeoNames、AreaCity 和 Modood 数据、表结构、许可证及全量构建方式见
[DATABASE.md](DATABASE.md)。

## 验证

```powershell
cd backend
python -m ruff check app tests scripts
python -m mypy app
python -m pytest -q

cd ../frontend
npm.cmd test
npm.cmd run build
```

单元测试使用 Fake Provider 和测试专用临时 SQLite；PostgreSQL 集成测试使用本地 `app` Schema，
不访问真实高德、12306、RollingGo、DIDA、Open-Meteo 或模型服务。真实服务冒烟测试必须与离线门禁分开执行。

## 并发实验室

当前单 Worker + PostgreSQL 的容量基线使用独立 Docker Compose、Fake Upstream 和 Locust 测量，
不读取真实密钥，也不修改生产 API。先执行 10 用户冒烟：

```powershell
.\loadtest.ps1 -Action Smoke -Scenario normal
```

完整的 10 → 50 → 200 → 500 用户阶段、故障场景、指标解释和受限真实供应商探针见
[CONCURRENCY.md](CONCURRENCY.md)。

## 当前边界

V0.5 仍是匿名访客、国内单目的地、单 Uvicorn Worker。长期记忆只保存用户明确授权的
常用出发地、旅行/饮食偏好、节奏、住宿档次和市内交通方式，不保存完整聊天、证件、订单、
日期、预算或供应商结果。当前不实现登录、支付、自动下单、多城市、真实省域/周边推荐、
RAG、多 Agent、语音或 PDF；OpenZLAgent 只通过公共接口复用，不承载旅行业务代码。
