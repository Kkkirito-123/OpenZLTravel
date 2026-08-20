# OpenZLTravel 架构

## 1. 设计目标

当前架构只解决一件事：用最少的权威状态和清晰的事实边界，完成单目的地旅行规划。

核心约束：

- 只有一个根图：`travel`。
- 只有一个执行状态：`TravelState`。
- 只有三个需要 LLM 的 Agent：RequirementAgent、PlannerAgent、ReviewAgent。
- 路由、目的地评分、Provider 查询、路线、预算、校验和保存都是确定性节点。
- Thread/Checkpoint 是唯一短期执行状态，Store 是唯一长期偏好和最终行程存储。
- LLM 不拥有事实写权限，任何最终保存都必须通过确定性校验。

## 2. 依赖方向

```text
domain
  ▲
  │
runtime/contracts ◄── providers / runtime/model_gateway
  ▲                         ▲
  │                         │
travel_graph            runtime/container
  ▲                         │
  └──── travel_graph/application ┘

api ──► runtime/config / api/identity / api/trips / domain
catalog_builder（独立子系统，不被 Graph 导入）
```

各层职责：

| 层 | 职责 | 禁止事项 |
|---|---|---|
| `domain/` | 领域模型、快速解析、确定性规划、事实校验 | 网络、环境变量、Store、LangGraph 路由 |
| `runtime/contracts.py` | Graph 所需的 Provider 与模型 Protocol | 构造具体实现、读取配置 |
| `travel_graph/` | Agent、节点、interrupt、状态 reducer、拓扑 | 解析原始供应商 JSON、另建会话状态 |
| `providers/` | 外部请求、响应解析、稳定事实 ID、缓存与重试 | 控制图路由、写 Store、调用 Agent |
| `runtime/container.py` | 组合配置、模型和具体 Provider | 承载业务规则 |
| `travel_graph/application.py` | 导出唯一编译图 | 定义第二套容器或第二个图 |
| `api/web.py` / `api/auth.py` | 身份初始化、历史接口、Agent Server 授权 | 保存执行中业务状态 |
| `catalog_builder/` | 独立构建和验证 PostGIS 地点目录 | 被 Graph 节点直接调用 |

## 3. 唯一状态

`TravelState` 是整个 Run 的权威状态：

| 字段 | 含义 | 主要写入者 |
|---|---|---|
| `messages` | LangGraph 标准消息，使用 `add_messages` reducer | Agent Server 输入 |
| `phase` | 当前固定阶段 | 各阶段入口节点 |
| `requirements` | 已确认或仍不完整的旅行需求 | 规则解析、RequirementAgent、clarification |
| `destination_candidates` | Catalog 确定性评分后的真实城市候选 | `recommend_destination` |
| `facts` | Provider 事实聚合，使用并行安全 reducer | Catalog、铁路、酒店、天气、路线节点 |
| `selection` | 用户选择的事实 ID 或自行安排标志 | `travel_selection` |
| `draft` | PlannerAgent 或确定性规划器的 ID-only 草稿 | `planner_agent` |
| `review` | ReviewAgent 的只读审查结果 | `review_agent` |
| `budget` | 真实报价与明示经验估算的确定性汇总 | `calculate_budget` |
| `trip_id` | 通过校验并保存后的稳定 ID | `save_trip` |
| `warnings` / `errors` | 可稳定展示的 `code/message/node` | 所有降级和失败节点 |
| `revision_count` | 审查修订次数，最大为 1 | `prepare_revision` |

固定阶段：

```text
collecting
  → discovering
  → awaiting_selection
  → planning
  → reviewing
  → completed

异常终态：failed / cancelled
```

短期执行状态只存在于 Thread/Checkpoint，业务代码不维护第二套会话 Repository。

## 4. 根图流程

```text
START
  │
  ▼
parse_requirement ──规则能理解──► requirement_guard
  │ 规则无法理解
  ▼
requirement_agent ───────────────► requirement_guard
                                      │
                 ┌────────────────────┼────────────────────┐
                 │缺字段              │只有地区             │具体城市已知
                 ▼                    ▼                    ▼
          clarification      recommend_destination   prepare_catalog
                 │                    │                    │
                 └──resume──► destination_selection        │
                                      │ resume             │
                                      └────────────────────┘
                                                           ▼
                                                   discovery_fanout
                                              ┌────────────┼────────────┐
                                              ▼            ▼            ▼
                                         fetch_rail   fetch_hotels  fetch_weather
                                              └────────────┼────────────┘
                                                           ▼
                                                    evidence_guard
                                                           ▼
                                                   travel_selection
                                                           │ resume
                                                           ▼
                                                    planner_agent
                                                           ▼
                                                     build_routes
                                                           ▼
                                                   calculate_budget
                                                           ▼
                                                     review_agent
                                                    ┌──────┴──────┐
                                                    │最多一次修订 │完成审查
                                                    ▼             ▼
                                             prepare_revision  final_validator
                                                    │             │
                                                    └► planner    ▼
                                                               save_trip
                                                                  ▼
                                                                 END
```

图路由全部由字段完整性、阶段和审查计数决定，LLM 输出不能指定下一节点。

## 5. 三个 Agent

### RequirementAgent

- 输入：当前消息、当前结构化需求、最近两轮消息。
- 输出：严格 `RequirementResult`，只含意图、需求补丁、缺失字段和置信度。
- 上限：384 Token，8 秒总截止时间。
- 降级：超时、未配置或结构错误都转为确定性追问，不返回 504。

### PlannerAgent

- 输入：完整需求、用户选择、经过裁剪的 Provider 事实、可选的一次修订指令。
- 输出：严格 `ItineraryDraft`。
- 事实限制：`poi_id`、`meal_ids`、`hotel_id` 必须逐字引用已提供的事实 ID。
- 上限：1800 Token，20 秒总截止时间。
- 降级：任何模型失败都使用可重现的确定性规划器。

### ReviewAgent

- 输入：需求、事实 ID 摘要和草稿。
- 输出：严格 `ReviewResult`，只能报告问题和修订指令。
- 上限：640 Token，8 秒总截止时间。
- 降级：模型失败时跳过语义审查，但仍执行选择、草稿、路线和事实 ID 校验。

三个 Agent 不共享完整聊天历史，也不能直接调用 Provider、Store 或图路由。

## 6. interrupt / resume

公开 interrupt 只有三类：

| `kind` | 何时出现 | resume 关键字段 |
|---|---|---|
| `clarification` | 进入发现前缺少必需需求 | `values: RequirementPatch` |
| `destination_selection` | 用户只指定了地区 | `candidate_id` |
| `travel_selection` | 铁路、酒店和天气发现完成 | `selection: TravelSelection` |

恢复节点先验证 `kind` 和严格 Pydantic 模型，再修改状态。错误载荷不会推进节点；同类
interrupt 会带着稳定的 `error.code` 和 `error.message` 再次返回。这个循环依赖 LangGraph
重放语义，因此 interrupt 调用顺序必须保持稳定。

## 7. Provider 与事实边界

Provider 返回的模型都是 `extra="forbid"` 的严格 Pydantic 事实。稳定 ID 由规范化输入和
SHA-256 摘要生成，避免不同 Run 中随机漂移。

绝对禁止由 LLM 创建或修改：

- 城市、POI、酒店名称和地址。
- 坐标、天气、车次、席别、票价和房价。
- 路线距离、时长、费用和 polyline。

最终校验会拒绝未知 POI、未知酒店、未知车次、未知席别和引用未知端点的路线。历史展示所需
的 `TripRecord.place_index` 只从 Catalog 与酒店 Provider 的事实确定性水合，PlannerAgent
仍然只输出 ID。

ProviderRuntime 统一提供：

- 小型异步 TTL 缓存。
- 相同 key 的进程内请求合并。
- 每 Provider semaphore。
- 单次硬超时。
- 最多一次、仅针对网络类错误的重试。
- 不泄露 Token 或密钥的稳定错误码。

外部适配器的选择也集中在 `runtime/config.py`：铁路默认使用 `Public12306Client`，直接
复用 12306 的车站编码、会话预热、余票和票价查询；需要替换为外部 MCP 时设置
`RAIL_PROVIDER=mcp`。酒店使用 RollingGo 的 Streamable HTTP MCP，固定调用
`searchHotels`，API Key 只从 `backend/.env` 的 `ROLLINGGO_API_KEY` 读取。两者失败时，
图节点仍会走明确的“自行安排”或本地目录降级，不让外部服务错误变成整张图的 500。

## 8. 目的地推荐

推荐必须已知出发地和地区，最多返回 5 个 Catalog 中真实存在的城市。评分固定为：

```text
景点覆盖         40%
偏好标签匹配     30%
距离匹配         20%
餐饮与住宿覆盖   10%
```

没有可靠城市成本数据时，预算不参与排名。数据库连接故障不会放大成批量高德请求。

## 9. Store、Checkpoint 与身份

短期执行状态：

- Thread 与 Checkpoint 由 LangGraph Agent Server 管理。
- Thread ID 是前端唯一工作流 ID，保存在浏览器 localStorage 以便恢复。
- 本地 `langgraph dev` 使用自定义 `InMemorySaver`；`JsonPlusSerializer` 只允许
  `TravelState` 实际包含的领域模型，且 `pickle_fallback=False`。开发启动默认开启
  `LANGGRAPH_STRICT_MSGPACK=true`，未登记类型必须在测试阶段暴露，而不能降级成字典。
- 内存 Checkpointer 只承诺单进程短期恢复；Agent Server 重启后前端会创建新 Thread，
  最终行程仍由独立 Store 保存。生产持久化 Checkpointer 不属于首版部署范围。

长期 Store：

```text
(user_id, "trips")        key = trip_id，保存最终 TripRecord
(user_id, "preferences")  key = stable，只保存明确授权的稳定偏好
```

稳定偏好只允许：出发地、旅行偏好、饮食偏好、节奏、住宿档次和市内交通。当前消息中的明确值
始终覆盖记忆；没有“记住/忘记”命令时不写 Store。

直接运行的 `AUTH_MODE=dev` 仍只接受 loopback，并使用固定 `dev-local`。
Docker 中的 Vite 代理会通过容器内网访问 Agent，因此统一 Compose 启动使用
签名模式：`POST /api/auth/anonymous` 设置 HMAC-SHA256、HttpOnly、SameSite=Lax
Cookie。认证上下文把 Thread、Run 与 Store 都限制到当前 owner；跨用户读取
统一表现为不可访问。

## 10. 前端

前端只有 `WorkbenchPage`，`useTravelThread` 是唯一状态入口：

- 初始化或恢复 Thread。
- 启动消息 Run 与 `Command(resume=...)`。
- 消费 `values` / `updates` 流式事件。
- 保存 Run ID 和 Last-Event-ID，支持断线续流。
- 处理三种 interrupt。
- 加载、查看和删除 Store 中的历史行程。
- “开始新行程”只创建新 Thread，不复用旧执行状态。

组件只接收结构化 props 和发出用户事件，不直接调用 SDK 或 HTTP。

## 11. Docker 运行边界

本地运行时只有一个 `compose.yml`，服务依赖是显式的：

```text
catalogdb --healthy--> agent --healthy--> frontend
   PostGIS              LangGraph          Vue / Vite
```

- `catalogdb` 复用固定命名的 `openzltravelcatalogdata` volume。
- `agent` 从 `backend/.env` 取得 Provider 和模型密钥，镜像本身不包含 `.env`。
- `agent` 通过 Docker DNS 访问 `catalogdb:5432`，并使用只读 `travelapp`。
- `frontend` 只将 `/api`、`/threads`、`/runs` 和 `/assistants` 代理到 `agent:2024`。
- 宿主机端口只绑定 `127.0.0.1`；Docker 内网代理使用签名 Cookie 认证，不放宽
  原有 `AUTH_MODE=dev` 的 loopback 校验。
- `start.ps1 down` 不删除 volume；Catalog 数据的创建与更新仍属于独立
  `catalog_builder` 子系统。

## 12. 代码与验收约束

- `backend/src` 核心代码不超过 7000 行。
- 源码按 `travel_graph / domain / providers / catalog / api / runtime` 分包，导入不得带
  `openzltravel` 前缀；本地图包不得命名为 `langgraph`，避免遮蔽官方依赖。
- 普通业务文件原则上不超过 400 行。
- 公共类、公共函数和关键事实/降级边界使用详细中文 docstring 或注释。
- `langgraph.json` 的 `graphs` 必须精确等于 `{"travel": ...}`。
- 核心运行时只能通过顶层契约访问 Provider、模型、Store 和 Catalog，不得绕过依赖容器。
- Fake Provider 的离线图测试、认证所有权测试和 Provider 解析测试不能为了减行而删除。

上述规则由 `backend/tests/test_architecture.py` 自动守护。
