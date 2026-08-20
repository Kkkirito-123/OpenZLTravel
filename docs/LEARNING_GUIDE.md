# OpenZLTravel LangGraph 新手学习指南

这份文档不是另一套架构说明，而是一条“从能看懂到能修改”的代码阅读路线。建议第一次
学习时不要从 Provider 或前端开始，而是先理解一个最小 LangGraph 工作流，再把同样的概念
映射到旅行规划项目。

## 1. 先记住 LangGraph 的五个概念

| 概念 | 在 LangGraph 中做什么 | 本项目对应位置 |
|---|---|---|
| State | 保存一次 Run 的共享数据 | `backend/src/travel_graph/state.py` |
| Node | 接收 State，返回 State 增量 | `travel_graph/nodes/`、`travel_graph/agents.py` |
| Edge | 决定下一个 Node | `travel_graph/workflow.py` |
| Reducer | 合并多个节点对同一字段的更新 | `state.py` 中的 `add_messages`、`merge_facts` |
| Checkpoint | 保存 State，使 Run 能暂停、恢复和重放 | `checkpoint.py` 与 Agent Server |

可以把一次图运行理解为：

```text
输入消息
   ↓
State
   ↓ Node 修改一小部分字段
State 增量 ──Reducer 合并──> 新 State
   ↓ Edge 路由
下一个 Node
```

本项目额外使用了 `interrupt`：当图需要用户选择时，Node 暂停；前端发送
`Command(resume=...)` 后，LangGraph 从 Checkpoint 恢复并重放该节点。

## 2. 项目树：先看职责，再看实现

```text
openzltravel/                         # 仓库名，不是 Python 导入前缀
├── langgraph.json                 # Agent Server 配置：只导出 travel 图
├── README.md                      # 项目总览与启动方式
├── ARCHITECTURE.md                # 完整架构约束和流程图
├── docs/
│   └── LEARNING_GUIDE.md          # 本文件：面向新手的阅读路线
│
├── backend/
│   ├── src/                        # Python 源码根目录
│   │   ├── travel_graph/            # 只放 LangGraph 相关代码
│   │   │   ├── application.py       # Agent Server 入口：导出 travel
│   │   │   ├── workflow.py          # 根图拓扑：节点和边
│   │   │   ├── state.py             # TravelState 和 reducer
│   │   │   ├── agents.py            # 三个受限 LLM Agent
│   │   │   ├── interrupts.py        # interrupt/resume 契约
│   │   │   ├── checkpoint.py        # Checkpoint 序列化和生命周期
│   │   │   └── nodes/               # 按图阶段拆分的节点
│   │   │       ├── requirement.py   # 需求解析、追问、目的地选择
│   │   │       ├── discovery.py     # Catalog、铁路、酒店、天气
│   │   │       ├── planning.py      # Planner、路线、预算
│   │   │       ├── review.py        # Review、修订路由、最终校验
│   │   │       └── persistence.py   # 最终校验后的 Store 保存
│   │   ├── domain/                  # 不依赖 LangGraph/网络的纯业务规则
│   │   ├── providers/               # 外部事实适配器与 Fake Provider
│   │   ├── catalog/                 # CatalogTool 与只读 SQL
│   │   ├── api/                     # Auth、FastAPI 和行程历史接口
│   │   └── runtime/                 # 配置、依赖装配、Protocol、模型网关
│   ├── catalog_builder/             # 独立 PostGIS 地点目录构建器
│   └── tests/                       # 图、Provider、认证和目录测试
│
└── frontend/src/
    ├── pages/WorkbenchPage.vue     # 唯一页面
    ├── composables/useTravelThread.ts # 唯一 Thread 状态入口
    ├── composables/travelThreadSupport.ts # 纯展示投影与消息归一化
    ├── services/travelGateway.ts   # LangGraph SDK / HTTP 远程编排
    ├── services/travelGatewaySupport.ts # 网络响应的纯数据归一化
    ├── components/                 # 只负责展示和发出事件
    └── types.ts                    # 前端契约投影
```

`backend/src` 下面直接按职责分包。看到 `travel_graph/` 就是图编排层；导入不包含
项目名，例如 `from travel_graph.state import TravelState`。

这里没有把每个函数拆成一个文件。文件夹表达“职责边界”，文件表达“一个相对完整的
协作单元”，这是当前项目刻意保持的粒度。

## 3. 推荐阅读顺序

### 第一步：看 Agent Server 入口

先打开 [langgraph.json](../langgraph.json)，只关注三个配置：

```json
{
  "graphs": { "travel": "./backend/src/travel_graph/application.py:travel" },
  "auth": { "path": "./backend/src/api/auth.py:auth" },
  "checkpointer": { "backend": "custom", "path": "...:create_checkpointer" }
}
```

这说明 Agent Server 只加载一个图，图名是 `travel`；认证和 Checkpoint 是平台边界，
不是业务节点。

然后看 [application.py](../backend/src/travel_graph/application.py)：它只有几行代码，因为
它是“组合根”，不应该塞入需求解析或 Provider 逻辑。

### 第二步：看 State

打开 [state.py](../backend/src/travel_graph/state.py)，重点看：

1. `TravelState` 的字段。
2. `messages` 使用 `add_messages`，所以新消息会按 LangGraph 规则合并。
3. `facts` 使用 `merge_facts`，因为铁路、酒店和天气节点会并行写不同字段。
4. `warnings` 和 `errors` 使用追加 reducer，保留每个节点的降级信息。

学习时可以先把 `TravelState` 当成一个带类型的 Python 字典。Node 不需要返回完整 State，
只返回自己负责的字段，例如：

```python
return {"phase": "discovering", "facts": TravelFacts(...)}
```

### 第三步：看根图拓扑

打开 [workflow.py](../backend/src/travel_graph/workflow.py)。阅读时只做两件事：

- 找 `add_node`：有哪些节点？
- 找 `add_edge` / `add_conditional_edges`：节点如何连接？

根图可以分成六段：

```text
需求解析
  → 需求补全/目的地选择
  → Catalog 和 Provider 并行发现
  → 用户选择车票/酒店
  → 规划、路线、预算
  → 审查、校验、保存
```

`route_after_requirement`、`route_after_catalog` 和 `route_after_review` 是普通 Python
函数；它们返回字符串，字符串再映射到下一个节点。这样路由是确定性的，不让 LLM 自己
决定跳到哪个节点。

### 第四步：按“需求 → 发现 → 规划”阅读节点

#### 需求节点

阅读 [requirement.py](../backend/src/travel_graph/nodes/requirement.py)：

- `parse`：先用规则解析当前消息，再加载明确授权的偏好。
- `recognize`：规则无法理解时才调用 RequirementAgent。
- `clarification`：字段缺失时调用 `interrupt`。
- `recommend` / `choose_destination`：地区型需求使用真实城市候选。

#### 发现节点

阅读 [discovery.py](../backend/src/travel_graph/nodes/discovery.py)：

- `prepare_catalog` 先准备城市和 POI 事实。
- `fetch_rail`、`fetch_hotels`、`fetch_weather` 是三个可并行节点。
- 每个 Provider 失败只写自己的 warning，不把整个图变成 500。
- `select_travel` 暂停等待用户提交稳定 ID。

#### 规划节点

阅读 [planning.py](../backend/src/travel_graph/nodes/planning.py) 和
[review.py](../backend/src/travel_graph/nodes/review.py)：

```text
plan
  → build_routes
  → budget
```

Planner 阶段只负责生成草稿并补充确定性路线、预算；审查阶段单独位于
`review.py`：

```text
  → review
  → final_validate
  → persistence.save
```

PlannerAgent 只产生草稿，`validation.py` 才决定草稿是否真的可以保存。这个边界是学习
本项目时最重要的设计：LLM 可以提出建议，但事实和副作用由确定性代码掌握。

### 第五步：理解三个 Agent

打开 [agents.py](../backend/src/travel_graph/agents.py)：

| Agent | 读取什么 | 产生什么 | 失败后怎么办 |
|---|---|---|---|
| RequirementAgent | 当前消息、需求、最近两轮 | `RequirementResult` | 确定性追问 |
| PlannerAgent | 需求、选择、事实 | `ItineraryDraft` | 确定性规划 |
| ReviewAgent | 需求、事实摘要、草稿 | `ReviewResult` | 跳过语义审查，继续确定性校验 |

三个 Agent 都通过 `_structured_call` 请求 Pydantic 结构化输出。没有“再调用一次模型修复
格式”的隐藏流程，超时和格式错误会被节点捕获并转成稳定 warning。

### 第六步：理解 interrupt 和 Checkpoint

打开 [interrupts.py](../backend/src/travel_graph/interrupts.py)：

```text
Node 调用 interrupt(payload)
        ↓
Run 暂停，payload 返回前端
        ↓
前端提交 Command(resume=payload)
        ↓
LangGraph 从 Checkpoint 重放 Node
        ↓
Node 验证 resume，验证通过才修改 State
```

本项目有三种 `kind`：

- `clarification`：补需求字段。
- `destination_selection`：选择推荐城市。
- `travel_selection`：选择车票和酒店。

`interrupt_until_valid` 为什么是循环？因为错误的恢复载荷不能推进图；它要返回同类型
interrupt，等待下一次输入。LangGraph 重放节点时，`interrupt` 的调用顺序必须保持一致。

## 4. 一次真实请求如何流过代码

假设用户输入：

```text
从上海去杭州玩 2 天，2026-12-10 到 2026-12-11，2 人
```

执行顺序大致如下：

1. `parse_requirement` 调用 `parse_fast_requirements` 提取城市、日期和人数。
2. `requirement_guard` 发现必填字段完整，跳过 RequirementAgent。
3. `prepare_catalog` 调用 CatalogTool，写入 `facts.city` 和 `facts.catalog`。
4. `discovery_fanout` 同时触发铁路、酒店、天气三个节点。
5. `evidence_guard` 汇合并行结果，进入 `travel_selection` interrupt。
6. 用户选择车票/酒店后，`planner_agent` 生成只引用事实 ID 的草稿。
7. `build_routes` 和 `calculate_budget` 使用确定性规则补充路线和预算。
8. `review_agent` 审查草稿，最多允许 Planner 修订一次。
9. `final_validator` 验证天数、地点 ID、酒店选择和路线端点。
10. `save_trip` 使用稳定 `trip_id` 幂等写入 Store，阶段变为 `completed`。

建议在测试 [test_travel_graph.py](../backend/tests/test_travel_graph.py) 中跟着这个顺序
设置断点。测试里的 Fake Provider 不访问网络，最适合学习。

## 5. 如何运行和观察

### 用 Docker 运行完整项目

```powershell
Copy-Item backend/.env.example backend/.env
Copy-Item backend/.env.catalog.example backend/.env.catalog.local
.\start.ps1
```

上面一个启动命令会创建三个职责清晰的容器：

1. `catalogdb`：保存真实城市、景点和餐饮目录。
2. `agent`：在容器内执行 `langgraph dev`，加载唯一的 `travel` 图。
3. `frontend`：运行 Vue 工作台，通过同源代理访问 Agent。

学习时可在 `backend/.env` 设置 `PROVIDER_MODE=fake`，然后执行
`.\start.ps1 restart`。这样图的 State、Node、Edge 和 interrupt 都与真实模式相同，
只是 Provider 不访问外部网络。

### 运行最有帮助的测试

```powershell
cd backend
python -m pytest tests/test_checkpointer.py -q
python -m pytest tests/test_travel_graph.py -q
python -m pytest tests/test_identity_auth.py tests/test_trip_http.py -q
```

### 学习时可以尝试的三个小改动

1. 在 `providers/fakes.py` 中增加一个景点，并观察确定性规划结果如何变化。
2. 在 `domain/parsing.py` 中增加一种明确的需求表达，再为它补一个测试。
3. 给 `test_travel_graph.py` 增加一个 Provider 失败测试，确认 warning 出现在 State 而不是
   直接抛出未处理异常。

每次修改都先写一个能复现行为的测试，再改代码；这样可以同时学习 LangGraph 和工程实践。

## 6. 初学者常见疑问

### 为什么不把所有逻辑都写在 Agent 里？

因为天气、价格、地点和路线必须可验证、可重放。Agent 适合理解自然语言和组织草稿，
不适合伪造事实或直接写数据库。

### 为什么 Node 不返回完整 State？

增量更新能明确每个节点负责哪些字段，也让并行节点可以通过 reducer 安全合并。

### 为什么要有 Fake Provider？

它让你不用 API Key、PostGIS 或真实网络，就能观察完整图流程并稳定运行测试。

### 为什么前端不直接调用每个 Node？

前端只提交消息或 resume；节点顺序、事实校验和保存副作用必须由根图统一控制。

## 7. 建议的学习顺序总结

```text
README
  → langgraph.json
  → travel_graph/application.py
  → travel_graph/state.py
  → travel_graph/workflow.py
  → travel_graph/nodes/requirement.py
  → travel_graph/nodes/discovery.py
  → travel_graph/nodes/planning.py
  → travel_graph/nodes/review.py
  → travel_graph/nodes/persistence.py
  → travel_graph/interrupts.py
  → travel_graph/agents.py
  → domain/validation.py
  → providers/fakes.py
  → tests/test_travel_graph.py
  → frontend/src/composables/useTravelThread.ts
```

如果这条路线能够读懂，你已经掌握了本项目最重要的 LangGraph 结构：状态、节点、边、
Reducer、Checkpoint、interrupt/resume，以及 Agent 与确定性业务代码的边界。
