# OpenZLTravel 开发约定

## 默认沟通

- 默认使用中文回复、计划、开发说明、代码注释和复盘。
- 先说明结论、假设和验收标准，再开始修改。
- 如果存在会改变结果的多种解释，必须明确列出；无法安全判断时先询问。

## 项目不变量

当前仓库是全新的 LangGraph Platform 架构，不兼容旧系统。

- `langgraph.json` 只允许导出一个名为 `travel` 的根图。
- `TravelState` 是唯一权威执行状态，不得新增 PlanningSession、DialogSession 或影子状态。
- LLM Agent 固定为 RequirementAgent、PlannerAgent、ReviewAgent 三个。
- 需求字段完整性、图路由、目的地评分、Provider 查询、路线、预算、校验和保存必须保持确定性。
- PlannerAgent 只能引用 Provider 事实 ID，不能创建地点、车票、酒店、天气、价格、坐标或路线。
- Thread/Checkpoint 保存短期状态；Store 只保存最终行程和明确授权的稳定偏好。
- 新核心不得导入 `app`、`re_zlagent`、Redis、SQLite 或 DIDA。
- `catalog_builder` 是独立子系统，Graph 只能依赖运行时 `CatalogTool`。

## 阅读顺序

```text
langgraph.json
  → backend/src/travel_graph/application.py
  → backend/src/travel_graph/state.py
  → backend/src/travel_graph/workflow.py
  → backend/src/travel_graph/nodes/
  → backend/src/travel_graph/agents.py
  → backend/src/travel_graph/interrupts.py
  → backend/src/travel_graph/checkpoint.py
  → backend/src/domain/
  → backend/src/providers/
  → backend/src/catalog/
  → backend/src/runtime/
  → backend/src/api/
  → frontend/src/composables/useTravelThread.ts
  → frontend/src/services/travelGateway.ts
  → frontend/src/pages/WorkbenchPage.vue
```

分层依赖和图流程见 `ARCHITECTURE.md`，不得只看单个节点就绕过既定边界。

## 1. 编码前先思考

不要静默假设，也不要隐藏不确定性。

- 明确这次修改允许触碰哪些文件、状态和外部系统。
- 把任务转换为可验证目标，例如“意图超时不再 504”应对应超时测试和稳定 interrupt。
- 如果有更简单的实现，先说明并选择最小方案。
- 如果一个改动会扩大公共契约、数据范围或外部副作用，先停下来确认。

多步骤任务使用简短计划：

```text
1. 修改边界 → 验证对应单元测试
2. 接入图节点 → 验证图级测试
3. 清理旧引用 → 验证 Ruff / Mypy / 全量测试
```

## 2. 简单优先

- 只写解决当前问题所需的最少代码。
- 不为单次使用创建抽象，不提前设计未要求的插件、配置或兼容层。
- 不保留旧 API、旧数据库、旧会话或迁移桥接“以防以后使用”。
- 能用 50 行清楚表达的逻辑，不要写成 200 行框架。
- 图拓扑显式优于动态 Supervisor；确定性分支优于让 LLM 选择工具或下一节点。

## 3. 精确修改

- 每一处修改都应能追溯到当前需求、测试或架构不变量。
- 不顺手整理无关代码、注释、格式或命名。
- 删除由本次修改产生的无用导入、变量、函数和文件。
- 发现无关死代码时先报告；只有用户明确要求清理时才删除。
- 保留用户本机 `.env`、`db/`、Token、离线原始数据和未提交工作，禁止读取或输出其内容。

本次全量重构已明确要求删除旧架构，因此以下版本控制路径不得恢复：

```text
backend/app/
backend/database/app.sql
backend/loadtests/
backend/scripts/
旧 requirements*.txt
旧前端多页面与旧测试
旧架构、数据库、并发和负载测试文档
```

## 4. 以验收结果驱动

实现后必须循环验证，不能以“代码看起来正确”结束。

- 修复 Bug：先复现或补回归测试，再修改，再确认测试只因正确原因通过。
- 重构：修改前确认当前行为，修改后运行同等级测试。
- 新边界：至少覆盖成功、失败、超时、无效输入和幂等场景中的相关部分。
- 外部 Provider：使用 Fake 或 MockTransport 验证请求参数和响应解析，不依赖真实网络作为唯一证据。
- Agent Server：最终必须真实启动 `langgraph dev`，文件可导入不代表 Agent Server 能加载。

## 状态与 interrupt 约定

- 新消息输入固定为 `{"messages":[{"role":"user","content":"..."}]}`。
- interrupt 判别字段固定为 `kind`，不是 `type`。
- 公开类型只有 `clarification`、`destination_selection`、`travel_selection`。
- 恢复统一使用 `Command(resume=ResumePayload)`，`kind` 必须与当前 interrupt 一致。
- 无效恢复不得推进状态，必须返回同类型 interrupt 的稳定 `error`。
- `warnings` 和 `errors` 使用 `GraphNotice(code, message, node)`，不混入任意字符串或异常对象。

## Agent 与事实边界

- RequirementAgent 只读当前消息、当前需求和最近两轮，8 秒超时后确定性追问。
- PlannerAgent 只读需求、选择和事实，20 秒超时后回退确定性规划器。
- ReviewAgent 只读需求、事实摘要和草稿，8 秒超时后跳过语义审查。
- Review 最多修订一次；不得增加无限自省、自循环或 Agent 间自由聊天。
- 任何 Agent 输出都要经过严格 Pydantic 结构化解析，不做第二次完整模型“修复调用”。
- 最终校验必须验证未知事实 ID、用户选择一致性、路线端点和日期结构。
- `TripRecord.place_index` 只能由 Provider 事实确定性水合，不能信任 Planner 生成的展示名称。

## Provider 约定

- 使用真正的异步 HTTP 客户端和连接池，不使用 `urllib + to_thread`。
- 每个 Provider 使用明确超时、semaphore、异步 TTL 缓存和最多一次网络重试。
- 只重试网络超时、连接错误和 5xx；认证、限流、业务错误和结构错误不盲目重试。
- 缓存键不得包含 API Key、Cookie、Bearer Token 或用户隐私。
- 未知票价、房价和天气保持未知，并产生明确警告；不得用 LLM 或经验值冒充实时数据。
- Catalog 的 12 位行政区编码在领域层保留，只有高德 HTTP 边界转换为 6 位编码。

## Store 与身份约定

- 最终行程命名空间固定为 `(user_id, "trips")`。
- 稳定偏好命名空间固定为 `(user_id, "preferences")`。
- 偏好只允许出发地、旅行/饮食偏好、节奏、住宿档次和市内交通。
- 只有用户明确“记住/忘记”才写删偏好；普通输入不产生长期副作用。
- `trip_id` 必须稳定且保存幂等，重复 Run 或恢复不得生成重复行程。
- 开发身份只允许 loopback；生产环境禁止 `AUTH_MODE=dev`。
- Thread、Run 和 Store 都必须按 owner 过滤，跨用户读取不能泄露资源存在性。

## 中文注释规范

- 公共 Python 类、公共函数、Protocol 方法、状态模型和公开载荷使用中文 docstring。
- 注释重点解释“为什么”：事实边界、重放约束、超时降级、幂等键和保存时机。
- 不给显然的赋值或语法逐行翻译，避免注释比代码更难维护。
- TypeScript 的公共类型、网关、composable 和断线恢复关键路径使用中文块注释。
- 修改行为时同步更新相关注释；过期注释视为缺陷。

## 文件与风格

- Python 目标版本为 3.11，不使用 Python 3.12 专属语法。
- 普通业务文件原则上不超过 400 行，核心 `backend/src` 不超过 7000 行。
- 源码包直接按职责放在 `backend/src`；导入中禁止使用 `src` 或 `openzltravel` 前缀。
- 本地图包固定命名为 `travel_graph`，不得命名为 `langgraph`，避免遮蔽官方依赖。
- 普通函数尽量不超过 40 行，嵌套不超过两层；复杂度上限由 Ruff C901 守护。
- Python 和普通 TypeScript 使用英文小写命名；Vue 组件使用 PascalCase。
- `styles.css` 只负责样式加载顺序，具体规则按职责拆分，单个样式文件保持可读。
- 组件不直接调用 SDK；所有 Thread/Run/历史状态集中在 `useTravelThread`。

## 验证命令

```powershell
cd backend
python -m ruff check src tests catalog_builder
python -m mypy
python -m pytest -q

cd ../frontend
npm.cmd test
npm.cmd run build
```

真实 Docker Agent Server 冒烟：

```powershell
cd ..
.\start.ps1
Invoke-WebRequest http://127.0.0.1:2024/ok -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:5173/ -UseBasicParsing
```

验证结果要区分“已通过”“未运行”“因外部环境阻塞”，不能把未执行描述为通过。
