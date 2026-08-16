# OpenZLTravel 开发约定

## 项目定位

OpenZLTravel V0.5 是独立旅行业务应用，通过公共接口复用 `re_zlagent` 的模型、上下文和
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
  → storage.py
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
- `providers/base.py`：MCP 生命周期、SQLite 缓存执行器、请求合并、重试和熔断。
- `providers/maps.py`：本地优先、异步调度和交通降级；高德 HTTP 在 `amap.py`，天气在
  `weather.py`，坐标与本地估算在 `geo.py`。
- `providers/rail.py`、`hotels.py`：供应商参数与稳定模型解析；酒店优先复用
  RollingGo Skill OAuth 令牌，旧 DIDA Token 仅作兼容。
- `providers/planner.py`：确定性规划和只允许改文案的 LLM 增强。
- `storage.py`：SQLite 行程、会话、缓存和本地目录。
- `models.py`：稳定公共类型，不依赖业务实现。

依赖方向：

```text
main → assistant → dialogue / skills / runtime / storage
assistant → re_zlagent 公共 Context / Conversation / Model 接口
main → runtime → workflow → travel / providers
runtime → storage / models
travel → models / storage
providers → config / models / errors / CacheStore
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
- RollingGo OAuth 令牌只从用户目录读取，不写入 SQLite、日志、API 响应或仓库。
- 酒店搜索和详情为只读能力；锁价、下单与支付必须经过独立接口和用户明确确认。
- 预算属于估算值；选定车票和酒店使用真实报价，缺失报价不计入总额并添加警告。
- 只有最终结构校验完成后才能保存行程，失败不得留下半成品。

## 稳定性约定

- MCP 客户端遵循 Streamable HTTP 初始化、通知、协议版本和会话 ID 生命周期。
- 同请求先查 SQLite 缓存，再做进程内任务合并；缓存键不得包含 Key 或 Token。
- 不把 SQLite 结果缓存称为 KV Cache；真实 KV Cache 由模型供应商持有，只能通过稳定提示
  前缀、可选 `prompt_cache_key` 和 cached token 指标使用与验证。
- 单用户单 Worker 不引入 Redis；只有多实例共享锁、队列、限流或 SQLite 写竞争成为真实
  需求时才实现 CacheStore 的 Redis 适配器。
- 每个 Provider 独立超时、限并发、最多一次网络重试和熔断。
- 认证失败、限流、业务错误和结构错误不重试。
- 会话状态只使用 `searching / awaiting_selection / generating / completed / failed /
  cancelled`；每个步骤独立记录耗时、尝试次数和降级信息。
- `Idempotency-Key` 防止重复创建；重复生成和重启恢复不能重复保存完整行程。
- 助手使用 `message_id` 幂等；相同 ID 的不同正文必须返回冲突，失败不得推进状态版本。
- 同一助手会话的消息在进程内串行合并，状态与幂等响应在同一 SQLite 事务保存。
- SQLite 保持单用户、单 Worker 边界，使用 WAL 和 busy timeout；不要引入新的数据库层。
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
- 不读取、输出或提交 `.env`、密钥、SQLite、`db/`、离线大数据和构建产物。

## 验证命令

```powershell
cd backend
python -m ruff check app tests
python -m mypy app
python -m pytest -q

cd ../frontend
npm.cmd test
npm.cmd run build
```

外部服务测试使用 Fake Provider 和临时数据库；真实联调与离线质量门禁分开报告。
