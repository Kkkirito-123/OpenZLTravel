# OpenZLTravel 助手认知层设计

本文描述 V0.5 已实现的意图识别、Skill、Token、缓存、上下文和记忆边界。目标是让旅行
助手可解释、可恢复、少调用模型，同时把地点、价格和路线事实留给确定性 Provider。

## 1. 总体流程

```text
用户消息
  ↓ message_id 幂等检查 + 单会话异步锁
快速解析（明确数字、日期、确认、取消、简单记住/忘记）
  ↓ 无法完整消费
构造专属上下文
  ↓
SQLite 精确意图缓存 → 进程内在途请求合并 → 受限 LLM Command Generator
  ↓
命令 Schema + 显式记忆授权 + 槽位业务校验
  ↓
TravelDialogueState 合并
  ↓
Skill Registry 选择确定性 Flow
  ├─ 信息不足：生成下一条追问
  ├─ 目的地发现：保存 recommendation_ready，不编造城市
  └─ 具体规划：构造 PlanningRequest，启动现有 PlanningRuntime
  ↓
状态、幂等响应和记忆变更在同一 SQLite 事务提交
  ↓
保存完整对话轮次，达到阈值后尝试滚动摘要
```

任何模型、摘要或 Provider 失败都不能留下半更新的任务状态。

## 2. 意图识别

LLM 是 Command Generator，不是执行 Agent。允许命令只有：

```text
StartFlow       选择 destination_discovery 或 trip_planning
SetSlot         设置当前消息明确给出的槽位
ClearSlot       清空用户明确撤销的槽位
Confirm         确认当前流程
CancelFlow      关闭当前需求
RouteToChat     返回能力边界提示
RememberSlot    显式保存一项稳定偏好
ForgetMemory    显式删除一项稳定偏好
```

模型不能输出工具名、SQL、供应商参数，也不能创建 POI、车次、酒店、价格、天气、路线或坐标。
结构不合法只允许一次修复。`RememberSlot` 必须在当前消息中出现“记住/以后都/下次默认”等
授权词；`ForgetMemory` 必须出现“忘记/不要记/清除/删除”，防止模型自行建立用户画像。

## 3. Skill 设计

`skills.py` 是静态注册表，不是任意 Python 插件系统：

| Skill | 输入事实 | 允许副作用 |
|---|---|---|
| 目的地需求发现 | 地区或出发地、主题偏好、天数、预算 | 只把状态推进到 `recommendation_ready` |
| 具体行程规划 | 出发城市、目的城市、日期或天数、预算 | 只启动 `PlanningRuntime` |

意图上下文只读取 Skill ID、所需槽位和副作用类型，不读取实现正文。新增 Skill 时必须先定义
输入 Schema、事实来源、允许副作用、失败降级和 Token 预算，再注册确定性处理器；不能让 LLM
自由拼工具调用。

## 4. 上下文分层

优先级由高到低：

1. 当前消息：单独作为本轮输入，永不被摘要替代。
2. `TravelDialogueState`：权威任务事实，包括槽位、状态和待回答字段。
3. 当前 Skill 契约：Host 可信元数据，无实现正文。
4. 显式长期偏好：仅作低优先级背景，带版本引用。
5. 最近最多 6 个完整轮次：不截断半个用户/助手轮次。
6. 滚动摘要：只辅助代词和历史目标理解，不能覆盖任务事实。

默认上下文字符预算为 5,000。每个片段有独立上限，`ContextManifest` 记录来源、可信度、
纳入字符、估算 Token 和是否截断；`ContextRef` 只保存无正文清单、轮次序号和 MemoryRef。

## 5. Token 控制

Token 控制分三层：

- 快速解析成功时不调用模型。
- 单次意图调用默认最多 2,048 输入、512 输出、8 秒；修复调用同样受限。
- 每个助手会话默认最多累计 20,000 Token，调用前按剩余额度收紧单次预算。

优先使用供应商 `usage`；供应商不返回时使用 OpenZLAgent 的保守估算。只累计成功进入状态
事务的意图调用，精确结果缓存命中记为零模型调用。滚动摘要由独立的 3,000 Token 近期窗口、
800 Token 摘要上限和 OpenZLAgent `ModelCallBudget` 控制，摘要失败不影响业务响应。

## 6. 缓存与 KV Cache

三种机制不能混称：

1. SQLite 结果缓存：对“模型、系统提示版本、当前消息和完整上下文”做 SHA-256，缓存已经
   校验的 Command，默认 TTL 1 小时。缓存不含 API Key，`evidence` 不落缓存。
2. 进程内请求合并：相同缓存键同时到达时共享一个 `asyncio.Task`，避免缓存写入前重复调用。
3. 模型端 Prompt/KV Cache：KV 实体只存在于模型服务端。对明确支持 OpenAI
   `prompt_cache_key` 的供应商，可配置 `INTENT_PROMPT_CACHE_KEY`；应用保持静态系统前缀并
   记录 `cached_input_tokens`。短提示低于供应商阈值时可能始终不命中，不能通过填充无用文本
   强行制造缓存。

默认关闭 `prompt_cache_key`，因为部分 OpenAI-compatible 服务会拒绝未知字段。

## 7. 记忆模型

| 层级 | 存储 | 生命周期 | 权威性 |
|---|---|---|---|
| 当前任务 | `travel_dialogue_sessions.state_json` | 一个助手会话 | 最高 |
| 原始轮次 | `conversation_turns` | 保留窗口内原文 | 仅辅助理解 |
| 滚动摘要 | `conversation_compactions` | 追加式版本 | 仅辅助理解 |
| 长期偏好 | `travel_memories` | 跨会话，直到用户删除 | 低优先级默认值 |

长期记忆仅允许：常用出发地、旅行偏好、饮食偏好、旅行节奏、住宿档次和市内交通方式。
日期、预算、同行人数、证件、订单、完整聊天和供应商结果不会自动保存。新会话复制长期偏好
作为 `source=memory` 的默认槽位；本轮 `source=user_explicit` 总是覆盖它。删除长期偏好不会
偷偷改写已经存在的会话快照。

## 8. 稳定性与事务

- `message_id` 相同且正文相同直接返回首次响应；正文不同返回 409。
- 每个会话使用异步锁串行合并，SQLite 再用 revision 乐观检查防止丢更新。
- 状态、幂等响应、Remember/Forget 在同一事务提交。
- 对话轮次与摘要在任务提交后保存；失败只记录告警，不回滚权威状态。
- 规划会话使用 `assistant:{session_id}:{revision}` 幂等键，重试不重复创建副作用。
- 日志和 ContextRef 不记录密钥、完整提示、记忆正文或供应商原始响应。

## 9. 为什么当前不使用 Redis

当前边界是本地单用户、单 Uvicorn Worker。SQLite WAL 已提供跨重启 TTL 缓存和持久会话，
进程内 Task 已合并并发请求。Redis 此时会增加安装、认证、连接池、数据过期一致性和新的
不可用路径，不能减少单次 LLM 推理时间。

调研参考：

- [TREK](https://github.com/liketrek/TREK) 的自托管单容器版本以 SQLite 为主，同时把地图、
  预算、预订和编辑拆成用户可控模块。
- [FloatTrip](https://github.com/shouzhuoshouzhuo/FloatTrip) 将 Redis 作为可选天气/POI 缓存，
  不可用时透传，并明确其 Runtime 是单节点边界。
- [zhilv-yuntu](https://github.com/tutu-zzz/zhilv-yuntu) 在加入多城市 RAG、Rerank 和容器化后
  使用 Redis 缓存天气、地图与检索；这是更重的数据检索场景。
- [trip-map-builder](https://github.com/hiyeshu/trip-map-builder) 只保存跨旅行仍有用的稳定偏好，
  不保存完整聊天、证件和订单，和本项目的记忆边界一致。

满足任一条件后再实现 Redis 适配器：多 Worker/多实例、需要跨实例锁或请求合并、后台队列、
分布式限流，或压测证明 SQLite 写锁成为瓶颈。迁移只替换现有 CacheStore/任务协调边界，
任务事实和最终行程仍应保存在持久数据库中。
