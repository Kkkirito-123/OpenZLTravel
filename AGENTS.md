# OpenZLTravel 开发约定

## 项目定位

OpenZLTravel V0.6 是独立旅行业务应用，通过公共接口复用 `re_zlagent` 的模型、上下文和
会话组件。LangGraph 只负责确定性节点编排，不代表多 Agent。不得修改 `src/re_zlagent/`
或把旅行业务放入 OpenZLAgent 核心。

## 阅读顺序

```text
main.py
  → assistant.py → dialogue.py → dialogue_commands.py / dialogue_context.py /
    dialogue_generator.py / dialogue_flow.py
  → skills.py
  → runtime.py
  → workflow.py
  → travel.py → travel_budget.py / travel_export.py
  → providers/base.py
  → providers/maps.py → amap.py / weather.py / geo.py
  → providers/rail.py / hotels.py / planner.py
  → catalog.py
  → storage.py → identity.py → coordination.py
  → models.py / errors.py / config.py
```

- `main.py`：HTTP、异常映射和依赖装配，不写业务规则。
- `assistant.py`：会话锁、消息幂等、OpenZLAgent 上下文记录和规划会话衔接。
- `dialogue.py`：兼容导出层；新代码按职责进入 `dialogue_commands.py`、`dialogue_context.py`、
  `dialogue_generator.py` 或 `dialogue_flow.py`，不访问供应商。
- `skills.py`：静态 Skill 契约和允许副作用，不动态加载代码或供应商参数。
- `runtime.py`：任务、幂等、恢复、取消和会话状态，不解析供应商响应。
- `workflow.py`：图结构和节点依赖，不保存最终行程。
- `travel.py`：事实组装、候选校验、编辑重算和行程读写；`travel_budget.py` 只处理
  经验预算与真实报价合并，`travel_export.py` 只处理 Markdown 导出。
- `providers/base.py`：MCP 生命周期、共享缓存执行器、请求合并、重试和熔断。
- `providers/maps.py`：本地优先、异步调度和交通降级；高德 HTTP 在 `amap.py`，天气在
  `weather.py`，坐标与本地估算在 `geo.py`。
- `providers/rail.py`、`hotels.py`：供应商参数与稳定模型解析；酒店优先复用
  RollingGo Skill OAuth 令牌，旧 DIDA Token 仅作兼容。
- `providers/planner.py`：确定性规划和只允许改文案的 LLM 增强。
- `catalog.py`：PostgreSQL 公共地点查询，不再提供生产 SQLite 回滚实现。
- `storage.py`：PostgreSQL 行程、规划和助手权威状态；不再负责地点目录或 Provider 缓存。
- `identity.py`：匿名 Cookie、Token 哈希、资源认领和所有权边界。
- `coordination.py`：集中 Redis Key、缓存、锁、限流、Provider 槽和任务租约；业务层不得直接拼 Key。
- `models.py`：稳定公共类型，不依赖业务实现。

依赖方向：

```text
main → assistant → dialogue / skills / runtime / storage / coordination
assistant → re_zlagent 公共 Context / Conversation / Model 接口
main → runtime → workflow → travel / providers
runtime → storage / models
travel → models / storage
providers → config / models / errors / coordination
catalog → PostgreSQL catalog / models / errors
storage → models
models → 无业务依赖
```

## 事实边界

- LLM 不能创建或修改地点、车票、酒店、天气、路线、价格和坐标。
- 意图 LLM 只能生成八种受限 Command；记忆写删还要通过显式授权校验，不能输出工具名、
  SQL 或供应商参数。
- `TravelDialogueState` 是权威任务事实；近期对话与摘要只能辅助理解，不能覆盖槽位。
- 意图阶段只加载无正文 Skill 契约，不得加载实现、POI、车票、酒店、地图或规划结果。
- 长期记忆只保存用户明确要求的稳定偏好；本轮明确值高于记忆，摘要永远不能写入记忆。
- 工作台由确定性规划器分天；LLM 只可润色摘要、主题和提示，失败立即回退模板。
- 旧快速入口的模型只能引用候选 `poi_id`，结构失败最多修复一次。
- 地图只绘制供应商返回的真实 `polyline`，不得用 POI 坐标画直线冒充道路。
- 领域坐标统一为 WGS-84；高德 Web API 和 JS 地图边界转换为 GCJ-02。
- 普通步行/驾车使用本地估算；公交、地铁和实时驾车经过统一高德调度与缓存。
- 未知票价、房价和天气保持未知，不得用模型或经验值填充。
- 第三方图片只保存合法 HTTP(S) URL，不下载、代理、缓存或放入模型提示。
- RollingGo OAuth 令牌只从用户目录读取，不写入 PostgreSQL、日志、API 响应或仓库。
- 酒店搜索和详情为只读能力；锁价、下单与支付必须经过独立接口和用户明确确认。
- 预算属于估算值；选定车票和酒店使用真实报价，缺失报价不计入总额并添加警告。
- 只有最终结构校验完成后才能保存行程，失败不得留下半成品。

## 稳定性约定

- MCP 客户端遵循 Streamable HTTP 初始化、通知、协议版本和会话 ID 生命周期。
- 同请求先查 Redis 共享缓存，再做进程内任务合并；缓存键不得包含 API Key 或 Token。
- 不把 Provider 结果缓存称为 KV Cache；真实 KV Cache 由模型供应商持有，只能通过稳定提示
  前缀、可选 `prompt_cache_key` 和 cached token 指标使用与验证。
- 地点查询和业务状态使用多人共享 PostgreSQL；生产默认四 Worker，由 Redis 协调共享临时状态。
- PostgreSQL 地点未命中允许高德兜底；数据库连接故障必须返回 `catalog_unavailable`，
  禁止把基础设施故障放大成批量高德请求。
- 每个 Provider 独立超时、限并发、最多一次网络重试和熔断。
- 认证失败、限流、业务错误和结构错误不重试。
- 会话状态只使用 `searching / awaiting_selection / generating / completed / failed /
  cancelled`；每个步骤独立记录耗时、尝试次数和降级信息。
- `Idempotency-Key` 防止重复创建；重复生成和重启恢复不能重复保存完整行程。
- 助手使用 `message_id` 幂等；相同 ID 的不同正文必须返回冲突，失败不得推进状态版本。
- 同一助手会话先取得 Redis 写锁，状态、幂等响应和长期偏好再在同一 PostgreSQL 事务保存。
- 浏览器 Cookie 只保存随机 Token，数据库只保存 Token 哈希；资源查询必须同时匹配 visitorid 和资源 ID。
- SQLite 只允许出现在一次性迁移工具和迁移测试中；生产 `app/` 不得导入 `sqlite3`。
- PostgreSQL 唯一约束是跨进程幂等最终保障；Redis 缓存只加速读取，不能替代唯一约束。
- Redis 锁和租约必须设置 TTL，值使用随机 Token，释放前必须比较 Token，避免误删新持有者的锁。
- Redis 普通缓存、访客缓存、幂等提示和 API 限流故障时允许回退；会话锁、Provider 槽和任务租约故障时必须拒绝执行。
- 日志允许记录请求 ID、会话 ID、步骤、耗时、缓存命中和稳定错误码，禁止记录密钥、
  完整上游响应或个人信息。

## 前端约定

- 助手与三阶段页面保持独立：`ChatPage`、`PlanPage`、`SessionPage`、`TripPage`；不要把
  状态集中到 `App.vue`。
- 页面属于安静、信息密集的工作台，不添加营销 Hero、装饰渐变或卡片嵌套。
- 使用 Lucide 图标；图标按钮必须有 `title` 或 `aria-label`。
- 点击区至少 44px；拖拽必须保留上移/下移按钮作为键盘与触屏替代。
- 每个外部步骤有独立加载、错误、降级和重试状态，长等待不得只显示整页转圈。
- 图片必须懒加载并处理失败占位；抽屉支持遮罩关闭、显式关闭和 Escape。
- 375px 下不得产生页面横向滚动；表格可在自己的容器内滚动。
- 动画使用 150～300ms，并遵循 `prefers-reduced-motion`。
- `styles.css` 只维护加载顺序；按页面或职责修改 `styles/` 下对应文件，避免重新堆回单一大文件。

## 代码风格

- Python 和普通 TypeScript 使用英文小写命名；Vue 组件使用 PascalCase 和 `*Page.vue`。
- 公共 Python 类和函数使用中文 docstring；注释解释事实边界、降级和保存时机。
- 优先早返回与单一职责，普通函数尽量不超过 40 行，嵌套不超过两层。
- Ruff 启用 `C901`，圈复杂度上限为 8；不为未批准的未来功能增加抽象。
- 修改应小而可验证，不顺手重构无关代码。
- 不读取、输出或提交 `.env`、密钥、SQLite、Redis 快照、`db/`、离线大数据和构建产物。

## 验证命令

```powershell
cd backend
python -m ruff check app tests scripts
python -m mypy app
python -m pytest -q

cd ../frontend
npm.cmd test
npm.cmd run build
```

外部服务测试使用 Fake Provider 和临时数据库；真实联调与离线质量门禁分开报告。
