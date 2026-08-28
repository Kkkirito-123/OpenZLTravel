# OpenZLTravel 代码流程

## 1. 两个独立运行单元

```text
Assistant Service                   Travel LangGraph
自然语言 + 事实工具                    已签名工单
      │                                   │
      └──── TravelOrderToken ─────────────┘
```

Assistant 负责交流、事实查询与选择确认；TravelGraph 负责验证已确认工单并执行确定性规划。
两者不共享 State、Checkpoint 或节点。

## 2. Assistant 调用链

```text
app.py
  → service.py
       ├─ intent.py          提取 AssistantDecision
       ├─ selection.py       校验并写入需求与选择
       ├─ agent.py           运行 LangChain create_agent
       │    └─ tools.py      暴露事实工具
       │         └─ fact_service.py → Provider Protocol
       └─ handoff.py         刷新事实并签发 TravelOrderToken
```

- `app.py`：身份认证、HTTP 和 SSE 响应边界。
- `service.py`：一轮请求的唯一编排入口。
- `intent.py`：结构化意图提取，不生成回复或调用旅行工具。
- `agent.py`：创建并运行 LangChain Agent，不签发工单。
- `tools.py`：只负责把事实服务适配为 LangChain 工具。
- `fact_service.py`：调用 Provider，并把验证后的事实写入当前快照。
- `handoff.py`：刷新车票、酒店和天气，构造并签发工单。

## 3. TravelGraph 调用链

```text
application.py
  → workflow.py
       validate_order
         → build_itinerary
         → build_routes
         → calculate_budget
         → validate_plan
         → route_preview
              ├─ 修改 → build_itinerary
              └─ 确认 → save_trip → END
```

- `state.py`：Graph Input、State 和 Context。
- `workflow.py`：唯一节点与边组合入口。
- `nodes/order.py`：验证工单签名、类型、所有者和事实边界。
- `nodes/planning.py`：日程、路线和预算节点。
- `nodes/confirmation.py`：最终校验与唯一 interrupt。
- `nodes/persistence.py`：幂等保存最终行程。
- `checkpoint.py`：运行状态的序列化与检查点实现。

## 4. 一次请求如何流动

1. Assistant 从签名 Session Token 恢复公开会话快照。
2. `IntentExtractor` 把当前输入转换为严格的 `AssistantDecision`。
3. `selection.py` 只接受已有事实 ID，更新需求和选择。
4. `ConversationRunner` 根据当前快照生成回复，并按需调用事实工具。
5. 用户明确开始规划后，`HandoffService` 刷新时间敏感事实并签发工单。
6. 前端新建 Travel Thread/Run，只向 Graph 提交 `order_token`。
7. Graph 验证工单，生成日程、查询最终路线并计算预算。
8. `route_preview` 暂停；修改会重新计算，确认后保存。

## 5. 状态与错误边界

- Assistant Snapshot 由浏览器携带，服务端使用 HMAC 验证后才恢复。
- TravelGraph State 只保存工单、事实、草稿、预算、警告和最终行程 ID。
- Provider 可降级问题写入 `warnings`；节点异常由 LangGraph Run 状态表示。
- `route_preview` 是业务中断，不是异常；通过 `Command(resume=...)` 恢复。
- Checkpoint 保存执行现场，不负责判断错误是否可以重试。

## 6. 验证入口

```powershell
cd backend
python -m pytest tests/test_assistant.py -q
python -m pytest tests/test_travel_graph.py -q
python -m pytest tests/test_checkpointer.py -q
```

- `test_assistant.py`：会话、工具、选择和 Token 安全。
- `test_travel_graph.py`：工单输入、路线修改、确认和幂等保存。
- `test_checkpointer.py`：严格序列化以及 interrupt/resume。
