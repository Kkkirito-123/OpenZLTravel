# OpenZLTravel 开发约定

## 默认沟通

- 默认使用中文回复、计划、开发说明、代码注释和复盘。
- 先说明假设、边界和可验证验收标准，再修改代码。
- 这是一次破坏性轻量化重构，不保留旧表单、旧输入或兼容层。

## 当前架构不变量

- `langgraph.json` 只导出一个名为 `travel` 的根图。
- `assistant/` 是独立 FastAPI + LangChain `create_agent` 服务；只负责对话、事实工具、筛选和签发工单。
- `travel_graph/` 只负责已签名 `TravelOrder` 的确定性规划执行，不读取 Assistant 会话、Graph State 或 Checkpoint。
- Assistant Session Token 和 TravelOrder Token 使用共享 HMAC 配置；后端不信任前端自造事实或选择。
- Assistant 查询的 POI、车票、酒店、天气事实必须带稳定 ID；提交工单前刷新时间敏感事实。
- TravelGraph 只接受 `{"order_token":"..."}`；旧消息输入、快速表单输入和旧 interrupt 均拒绝。
- 图流程固定为：`validate_order → build_itinerary → build_routes → calculate_budget → validate_plan → route_preview → save_trip`。
- 只保留 `route_preview` interrupt；修改仅支持“某景点移到第 N 天”和“第 N 天少安排一个”。
- Graph 不重新查询 POI、车票、酒店或天气；只在每日顺序生成后查询最终路线。
- Store 只保存最终行程，主键基于 `user_id + order_id`，重复提交必须幂等。
- 不新增业务数据库、Redis、任务队列或长期用户画像。

## 阅读顺序

```text
langgraph.json
  → backend/src/assistant/app.py
  → backend/src/assistant/service.py
  → backend/src/assistant/tools.py
  → backend/src/runtime/tokens.py
  → backend/src/travel_graph/application.py
  → backend/src/travel_graph/state.py
  → backend/src/travel_graph/workflow.py
  → backend/src/travel_graph/nodes/
  → backend/src/domain/
  → backend/src/providers/ 与 backend/src/catalog/
  → frontend/src/pages/AssistantPage.vue
  → frontend/src/composables/useAssistantWorkspace.ts
  → frontend/src/services/assistantGateway.ts
  → frontend/src/services/planningGateway.ts
```

## 分层边界

| 层 | 负责 | 不负责 |
|---|---|---|
| `domain/` | 模型、确定性规划、预算、事实校验 | 网络、环境变量、Graph 路由 |
| `assistant/` | 自然语言、只读工具、会话快照、工单交接 | 写 Store、运行 TravelGraph |
| `providers/` / `catalog/` | 地点、铁路、酒店、天气、路线事实 | 控制 Graph 路由、调用 Agent |
| `runtime/` | 配置、Protocol、依赖装配、签名 Token | 承载业务状态 |
| `travel_graph/` | 工单验证、规划、路线、预算、确认、保存 | 需求收集、事实发现、旧表单 |
| `api/` | 匿名身份、历史行程和 Agent Server 授权 | 保存执行中会话 |
| `frontend/` | 唯一 AI 页面、SSE、Thread/Run 交接与展示 | 解析自然语言、制造事实 |

## 代码规则

- 只写当前需求所需的最少代码；不为旧 API 保留别名或兼容分支。
- 领域和图节点不得导入具体 Provider 实现；通过 `runtime.contracts` 依赖协议。
- 公开 Python/TypeScript 类型和边界函数使用中文 docstring/注释。
- 核心模块必须在文件头说明“职责、输入、输出和禁止事项”；不要只写“工具类”“服务类”等空泛描述。
- Assistant/Graph 边界函数必须注明状态来源、状态写入位置、Token/身份校验和失败后的状态变化。
- Graph 节点注释必须列出读取字段、写入字段、外部端口和下一跳；凡是 interrupt 都要说明可接受的恢复命令。
- Provider 注释必须说明事实来源、缓存期限、降级行为和未知值如何保留；禁止用“默认值”掩盖上游缺失。
- 前端 Gateway 注释必须说明请求是否创建新 Thread/Run、是否携带签名 Token，以及断线恢复是否幂等。
- 注释解释业务不变量和决策原因，不复述显而易见的赋值；代码行为变化时必须同步更新注释。
- 普通 Python 文件原则上不超过 400 行；核心源码不超过 7600 行。
- Provider 的未知价格、天气和库存必须保持未知并明确降级，不得由模型臆造。
- 图状态只保留工单、事实、草稿、路线、预算、警告错误和 `trip_id`。

## 验证命令

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

未实际运行的 Docker/外部 Provider 检查必须明确标记为未运行或被环境阻塞，不能声称已通过。
