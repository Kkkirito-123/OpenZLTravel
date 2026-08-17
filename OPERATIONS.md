# OpenZLTravel 运行维护

OpenZLTravel 当前面向匿名访客、单 Uvicorn Worker。PostgreSQL 同时保存行程、规划会话、
对话状态和带 TTL 的 Provider 缓存；Redis 多 Worker 协调属于后续 PR。

## 启动

首次安装并启动开发环境：

```powershell
cd C:\Users\14405\Desktop\OpenZLAgent-refactor\examples\openzltravel
.\start.ps1 -Install
```

脚本启动后端 `http://127.0.0.1:8000`，前端由 Vite 选择可用的本机端口。12306 MCP 是独立
只读服务，需要单独启动；未启动时页面会显示车票步骤不可用，用户仍可选择“自行安排”。

执行 `npm.cmd install` 或 `npm.cmd audit fix` 后，需要停止并重新启动 Vite 开发服务。Vite
可以热更新应用源码，但不会可靠地替换自身已经加载的内部依赖文件。

```powershell
python -m pip install mcp-server-12306
$env:SERVER_HOST = "127.0.0.1"
$env:SERVER_PORT = "8001"
mcp-12306
```

## 首次 PostgreSQL 初始化与旧数据迁移

```powershell
cd C:\Users\14405\Desktop\OpenZLAgent-refactor\examples\openzltravel
.\catalog.ps1 -Runtime

# 先停止仍在写旧 SQLite 的后端，再执行一次性只读迁移
cd backend
python -m scripts.migrate_sqlite_to_postgres db\openzltravel.sqlite3 `
  --claim-output db\legacy-claim.txt
```

`-Runtime` 会创建 `app` Schema、授予 `travelapp` 权限，并把 `DATABASE_URL` 写入被 Git
忽略的 `.env.runtime.local`。迁移以源文件 SHA-256 防重复，任何表失败都会整体回滚；原
SQLite 不修改、不删除。认领码有效 24 小时，通过 `POST /api/visitor/claim` 使用一次。

## 就绪检查

`/health` 只确认 HTTP 服务存活；`/ready` 不访问外网，展示本地依赖与配置状态。

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

预期重点字段：

| 字段 | 正常值 | 异常后的可用行为 |
|---|---|---|
| `database` | `ready` | 不能创建或恢复会话，应先修复 `DATABASE_URL`、数据库权限和连接池。 |
| `catalog` | `ready` | `missing` 时城市/POI 会按配置尝试高德兜底。 |
| `rail_mcp` | `configured` | 仅表示地址已配置，不代表 12306 服务已启动。 |
| `hotel_provider` | `rollinggo_oauth` 或 `dida_token` | 未登录时使用本地 OSM 酒店。 |
| `intent_model` | `configured` | 缺失时聊天页不能识别意图，`/plan` 仍可用。 |

## 日常观察

后端日志会记录请求 ID、规划会话 ID、步骤状态、耗时、缓存命中和稳定错误码，不记录 API
Key、OAuth 令牌、完整上游响应或用户完整对话。排查一次任务时，先按页面或 API 响应里的
`session_id` 查找同一会话的 `planning_step` 和 `planning_session_failed` 日志。

规划会话状态的含义：

```text
searching → awaiting_selection → generating → completed
                     └──────────→ failed → retry
任意未完成状态 → cancelled
```

- `searching` / `generating` 在正常服务重启后自动恢复。
- `completed` 已经保存完整行程，重启恢复不会重复写入。
- `cancelled` 是终态，不能被迟到的后台任务重新推进。
- Provider 缓存命中和进程内相同请求合并是正常优化，不代表外部数据永久最新。

## 常见故障

| 页面或错误码 | 原因 | 处理 |
|---|---|---|
| `amap_rate_limited` | 高德 QPS 或配额触发限制 | 等待冷却时间；普通路线会使用本地估算，避免连续刷新同一请求。 |
| `amap_not_configured` | 后端未配置高德 Key | 本地目录命中时城市/POI 仍可用；配置 Key 前不要依赖高德兜底、公交或实时驾车。 |
| `weather_unavailable` | Open-Meteo 与高德天气均不可用 | 行程保留，日期显示“暂无预报”。 |
| `rail_*` | 12306 MCP 未运行、超时或预售范围外 | 检查 `RAIL_MCP_URL` 和 MCP 进程；可选择自行安排往返。 |
| `rollinggo_login_required` | 本机 RollingGo OAuth 令牌不存在或失效 | 执行 `rgh.cmd login`；住宿搜索会回退本地酒店。 |
| `intent_not_configured` / `intent_timeout` | LLM 未配置或响应慢 | 检查 LLM 配置与网络；使用 `/plan` 继续表单规划。 |
| `database_unavailable` | PostgreSQL 连接池不可用 | 检查 `DATABASE_URL`、PostgreSQL 容器和 `/ready`，不要直接放大高德兜底流量。 |

不要通过无限重试规避限流或认证错误。认证失败、业务错误和结构错误在代码中被设计为不重试，
以免重复消耗 Key 或造成重复任务。

## 安全与备份

- `backend/.env`、`frontend/.env`、RollingGo OAuth 令牌和数据库备份均不提交 Git。
- 日志、截图和问题报告不要粘贴真实 Key、用户对话、订单信息或原始供应商响应。
- PostgreSQL 备份使用 `pg_dump`；旧 SQLite 只在迁移前保留只读原文件，不在运行时继续写入。
- 公开数据目录和缓存可以重新生成；行程与会话数据库是需要保护的用户数据。

## 发布前验证

离线质量门禁不读取真实密钥、不调用外部供应商：

```powershell
cd backend
python -m ruff check app tests scripts
python -m mypy app
python -m pytest -q

cd ..\frontend
npm.cmd test -- --run
npm.cmd run build
npm.cmd audit --omit=dev
```

真实联调另行执行：确认 `/ready`、创建一条短行程、检查车票/酒店降级文案、确认地图仅绘制
真实 `polyline`，再删除该测试行程。真实联调结果不能替代离线测试结果。
