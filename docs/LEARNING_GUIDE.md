# OpenZLTravel 学习指南

## 1. 先理解两个独立运行单元

```text
Assistant Service                   Travel LangGraph
自然语言 + 工具                     已签名工单
      │                                   │
      └──── TravelOrderToken ─────────────┘
```

Assistant 负责“聊什么、查什么、选什么”；TravelGraph 负责“按已确认事实如何执行”。两者不共享
State、Checkpoint 或节点。

## 2. 推荐阅读顺序

### Assistant

1. `backend/src/assistant/models.py`：一轮请求、公开快照、Action 和 Handoff。
2. `backend/src/assistant/app.py`：匿名身份认证和 SSE 事件边界。
3. `backend/src/assistant/service.py`：从签名 Token 恢复快照、用 LLM 理解资料、调用 Agent 和签发工单。
4. `backend/src/assistant/tools.py`：Catalog、铁路、酒店、天气工具怎样写入权威事实。
5. `backend/src/assistant/selection.py`：卡片与自然语言如何统一校验已有事实 ID。
6. `backend/src/runtime/tokens.py`：类型、用户、签发和过期时间如何进入 HMAC Token。

### TravelGraph

1. `backend/src/travel_graph/state.py`：图状态只剩工单、事实、草稿、预算和结果。
2. `backend/src/travel_graph/workflow.py`：顺序固定，没有 Supervisor 和需求阶段。
3. `nodes/order.py`：只接受 `order_token`。
4. `nodes/planning.py`：确定性日程、最终路线和预算。
5. `nodes/confirmation.py`：唯一 `route_preview` interrupt 和受限修改。
6. `nodes/persistence.py`：`user_id + order_id` 幂等保存。

### 前端

1. `frontend/src/pages/AssistantPage.vue`：唯一页面和展示组合。
2. `frontend/src/composables/useAssistantWorkspace.ts`：浏览器会话、SSE、Thread/Run、断线恢复和历史。
3. `frontend/src/services/assistantGateway.ts`：Assistant SSE 解析。
4. `frontend/src/services/planningGateway.ts`：LangGraph SDK 工单启动和 interrupt 恢复。

## 3. 一次请求如何流动

假设用户说：“从上海去杭州，10 月 1 日到 3 日，2 人预算 5000 元”。

1. LLM 以 JSON 模式理解用户资料，LangChain Agent 自主生成回复并决定是否调用工具。
2. Agent 结合上下文自然交流；服务端不再用固定字段顺序或模板覆盖模型回复。
3. 用户通过卡片或自然语言选择景点，服务端验证 ID；Agent 按需查询车票、酒店和天气。
4. 所有选择完成后状态变为 `ready`。
5. 用户说“开始规划”，Assistant 刷新车票、酒店和天气；失效就返回新候选。
6. Assistant 签发 `TravelOrderToken`，前端新建 Travel Thread/Run。
7. Graph 验证 Token，确定性生成每日行程，然后按实际每日顺序查询路线并计算预算。
8. `route_preview` 暂停。受支持的修改会回到 `build_itinerary`；确认后保存。

## 4. 为什么这样拆分

- 需求讨论需要自然语言和工具编排，适合 Agent。
- 最终规划需要可重放、可测试和可校验，适合确定性领域函数。
- 价格和库存必须在交接前刷新，不能让 Graph 持有长时间失效的事实发现流程。
- Graph 输入只有签名 Token，可以明确拒绝前端篡改事实和跨用户工单。
- 单一 interrupt 让 Checkpoint 恢复协议保持简单。

## 5. 从测试学习

```powershell
cd backend
python -m pytest tests/test_assistant.py -q
python -m pytest tests/test_travel_graph.py -q
python -m pytest tests/test_checkpointer.py -q
```

- `test_assistant.py`：看会话、工具、选择和 Token 安全。
- `test_travel_graph.py`：看工单输入、路线修改、确认和幂等保存。
- `test_checkpointer.py`：看 `route_preview` 重放依赖的严格序列化。

学习时最安全的小改动是：给 Fake Catalog 增加一个 POI，再观察 Assistant 卡片和确定性日程如何变化。
