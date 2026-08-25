# OpenZLTravel 轻量架构

## 1. 总览

```text
Vue 唯一 AI 页面
  ├─ POST /api/assistant/turn + SSE
  │    └─ Assistant Service
  │         ├─ LangChain create_agent
  │         ├─ 收集需求、推荐城市/POI
  │         ├─ 查询车票、酒店、天气
  │         ├─ 校验卡片与自然语言选择
  │         └─ 签发 TravelOrderToken
  │
  └─ handoff.ready → 新建 Travel Thread/Run
       └─ Travel LangGraph
            validate_order
              → build_itinerary
              → build_routes
              → calculate_budget
              → validate_plan
              → route_preview
                   ├─ 修改 → build_itinerary
                   └─ 确认 → save_trip → END
```

Assistant 与 TravelGraph 业务隔离：不共享 Graph State、Checkpoint 或节点。LangChain Agent
底层使用 LangGraph runtime，但它只是对话 Agent 框架，不是旅行规划图。

## 2. 分层职责

| 层 | 职责 |
|---|---|
| `backend/src/openzltravel/assistant/` | 独立 LangChain 服务：会话恢复、Agent、只读工具、SSE 和工单签发 |
| `backend/src/openzltravel/travel_graph/` | 独立 LangGraph 服务：工单认证、日程、路线、预算、确认、保存和 HTTP API |
| `backend/src/openzltravel/domain/` | 两个服务共享的 TravelOrder、事实模型、确定性规划和校验 |
| `backend/src/openzltravel/infrastructure/` | Catalog、12306、RollingGo、天气和路线 Provider 适配器 |
| `backend/src/openzltravel/runtime/` | Settings、Protocol、身份、依赖装配和 HMAC Token |
| `frontend/src/features/assistant/` | Assistant 页面、SSE Gateway、卡片和浏览器会话 |
| `frontend/src/features/planning/` | Graph Thread/Run、路线确认和最终行程 |
| `frontend/src/features/trips/` | 历史行程 |

Graph 只依赖 `PlanningDependencies.routes`。Assistant 才依赖 Catalog、铁路、酒店和天气。

## 3. 会话与工单

Assistant 没有服务端会话数据库。浏览器 `sessionStorage` 保存：

- 公开 `AssistantSnapshot`：最近最多 20 轮消息、有限候选、已选事实。
- HMAC 签名 `session_token`：绑定当前匿名用户、类型和过期时间。

后端每轮从签名快照恢复，并校验卡片/自然语言命中的事实 ID。LLM 只能引用已经由工具写入
快照的事实，不能创建 POI、车票、酒店、价格或天气。

每轮由 LLM JSON 模式更新内部结构化资料，再由 LangChain `create_agent` 自主组织回复和调用
只读工具。服务端只做事实 ID 校验、状态派生和工单安全边界，不按固定字段顺序生成追问，也不
在模型失败时静默降级成规则对话；模型不可用会返回明确的 `assistant_model_error`。

用户说“开始规划”时，Assistant 刷新车票、酒店和天气；若库存失效，返回新候选而不签发工单。
刷新成功后构造完整 `TravelOrder`，包含：

- `order_id`、创建时间、事实刷新时间；
- 出发地、目的地、日期、人数、预算、节奏和偏好；
- 景点、去返程车次与席别、酒店；
- POI、车票、酒店、天气事实及 `FactStamp` 来源时间。

`TravelOrderToken` 绑定 `user_id + order_id`、短时有效且只允许 Graph 验证。篡改、过期、类型
错误或跨用户 Token 均拒绝运行。

## 4. TravelGraph 状态与边界

`TravelState` 只保留：

```text
phase / order / facts / draft / budget
route_revision_instruction / warnings / errors / trip_id
```

Graph 不接收旧 `messages`、快速表单或选择 interrupt。`validate_order` 只验证 Order Token 和
选择事实边界；后续规划节点均为确定性函数。最终路线在每日活动顺序确定后才调用 Route Provider。

`route_preview` 是唯一 interrupt：

- `confirm`：预算允许时保存；超预算必须显式 `allow_over_budget`。
- `message`：只接受“把某景点放到第 N 天”或“第 N 天少安排一个”；无法识别时返回同一预览和明确错误。

保存主键由 `uuid5(user_id + order_id)` 派生，Store 命名空间固定为 `(user_id, "trips")`，重复
运行只返回已有 `trip_id`。

## 5. SSE 与前端交接

`POST /api/assistant/turn` 请求只接受 `message` 或 `action` 其中一个。事件为：

```text
message.delta
tool.started
tool.result
session.updated
handoff.ready
error
done
```

前端收到 `handoff.ready` 后调用 LangGraph SDK 新建 Thread，并以
`{"order_token":"..."}` 启动 Run。规划流支持 `route_preview` 恢复、断线重连、历史行程读取
和最终行程展示。页面不显示需求表单，所有字段通过自然语言和卡片内置维护。

## 6. 事实边界

- Assistant 工具均为只读 Provider 查询；工具结果先写入服务端验证过的签名快照。
- Graph 不重新查询 POI、车票、酒店或天气，不接受客户端事实对象。
- Graph 只在最终每日顺序生成后查询路线，路线端点必须来自工单 POI ID。
- 未知票价、房价、天气和路线保持未知，必须有明确 warning，不用模型补造。

## 7. 验收

后端覆盖 Assistant 追问、工具降级、卡片/自然语言选择、令牌安全；Graph 覆盖旧输入拒绝、
路线修改、预算确认、跨用户拒绝和幂等保存。前端只保留历史抽屉和最终行程组件测试。

```powershell
cd backend; python -m ruff check src tests catalog_builder; python -m mypy; python -m pytest -q
cd ../frontend; npm.cmd test; npm.cmd run build
```

## 8. 固定 Benchmark

`backend/benchmarks/` 保存版本化的 30 个 Assistant/TravelGraph 用例。功能层使用固定
Fixture、Fake Provider 和 Replay Model，质量层在显式配置 LangSmith 与 LLM 密钥后运行；两层
都不改变生产服务的状态或 Provider 依赖。运行命令为：

```powershell
cd backend
python -m benchmarks.run --suite functional --report reports/functional.json
python -m benchmarks.run --suite quality --report reports/quality.json
```

功能层报告字段、工具、工单、安全、Graph 完成和幂等结果；质量层按固定 rubric 评分回复
内容。报告模式只记录失败，不阻断发布。
