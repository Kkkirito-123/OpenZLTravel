# OpenZLTravel 并发实验室

## 本阶段回答什么

本 PR 只建立测量能力，不修复测量出的瓶颈，也不引入 Redis、PostgreSQL 业务迁移、
多 Worker 或新的生产 API。实验环境固定为：

```text
Locust
  → OpenZLTravel：单 Uvicorn Worker + SQLite WAL
      → Fake 12306 / 酒店 / 高德 / Open-Meteo / LLM
```

运行网络设置为 Docker `internal`，容器无法访问公网，因此 500 用户实验不会误用真实 Key。
OpenZLAgent 依赖固定到提交 `3e1371b17331f9349973dfbfcd5e92ad16d1ccac`，避免基线随上游变化。
压测服务也不向宿主机发布端口；调试时通过 Compose 日志和生成的结果文件观察。
SQLite 使用 Docker Linux 命名卷，避免 Windows 文件共享层改变锁与 fsync 行为；测试停止后再把
数据库快照复制到结果目录。

## 五种故障场景

| 场景 | 用途 |
|---|---|
| `normal` | 所有 Fake Provider 正常返回，用于吞吐基线 |
| `slowllm` | LLM 延迟超过应用截止时间，观察聊天与其他步骤是否互相拖累 |
| `raillimit` | 12306 MCP 工具调用返回 429，验证铁路步骤独立降级 |
| `amaptimeout` | 高德响应超过应用超时，验证本地数据缺失时的失败边界 |
| `mixedfailure` | 按固定序号注入 429 与 503，验证错误隔离和报告完整性 |

Fake 服务仅在压测容器中提供：

```text
GET  /health
GET  /stats
POST /reset
POST /scenario
```

`/stats` 不保存请求正文、Key、Token 或供应商完整响应。

## 使用方式

首次构建并执行 10 用户、30 秒冒烟：

```powershell
.\loadtest.ps1 -Action Smoke -Scenario normal
```

执行默认四阶段基线：

```powershell
.\loadtest.ps1 -Action Run -Scenario normal
```

默认阶段：

```text
10 用户 × 30 秒
50 用户 × 60 秒
200 用户 × 90 秒
500 用户 × 120 秒
```

自定义阶段：

```powershell
.\loadtest.ps1 -Action Run -Scenario slowllm -Stages "10:30,50:60"
```

查看最近报告或停止环境：

```powershell
.\loadtest.ps1 -Action Results
.\loadtest.ps1 -Action Stop
```

结果写入被 Git 忽略的 `loadtests/results/<时间>-<场景>/`，包括 Locust CSV、Fake 调用
统计、业务计数、SQLite 快照和 `summary.md`。

## 流量模型

| 比例 | 用户行为 |
|---:|---|
| 40% | 健康检查、规划会话状态读取 |
| 25% | 创建助手会话并发送两轮模糊消息 |
| 25% | 创建规划会话并轮询发现进度 |
| 10% | 重复提交、取消和重试 |

正常请求使用唯一幂等键和独立会话。只有重复提交任务复用同一个键，用于确认双击不会
创建两个规划会话。所有行程日期在运行时生成，始终位于未来且不超过七天。

## 指标解释

每轮报告至少包含：

- 请求总量、RPS、p50、p95、p99、HTTP 失败；
- SQLite locked、任务创建/完成、重复行程；
- Fake Provider 实际调用、429、失败和平均耗时；
- 规划步骤平均耗时、缓存命中率；
- “缓存或请求合并避免调用估算”。

最后一项是实验估算：在压测容器没有本地 catalog 时，一次冷发现理论上会产生高德 4 次、
铁路 8 次、酒店 1 次、天气 1 次调用。理论值与 Fake 实际值的差包含 SQLite 缓存和进程内
同请求合并，不能当作精确计费数据。后续 PR 若增加正式指标接口，再拆分两者。

## 真实供应商只读探针

真实探针默认拒绝运行，必须显式确认：

```powershell
.\loadtest.ps1 -Action Probe -Provider openmeteo -ConfirmLive
```

安全边界：

| 供应商 | 并发上限 | 总调用上限 | 说明 |
|---|---:|---:|---|
| 12306 MCP | 1 | 6 | Beta 项目无公开 QPS；仅查车站，不做并发压极限 |
| 高德 | `min(2, AMAP_ACCOUNT_QPS)` | 6 | QPS 必须从控制台读取，代码默认按 1 |
| Open-Meteo | 1 | 3 | 只测少量延迟，不做真实并发压测 |
| RollingGo / DIDA | 2 | 6 | 未登录或无 Token 时跳过 |
| LLM | 2 | 6 | 还需显式设置 `LLM_PROBE_ENABLED=true` |

公开容量信息：

- [高德配额说明](https://lbs.amap.com/upgrade)：基础 LBS 月配额为个人认证 15 万、企业认证
  300 万、技术许可企业 900 万；天气个人与普通企业均为每月 5000。
- [高德流量限制](https://lbs.amap.com/api/webservice/guide/tools/flowlevel)：具体 QPS 以控制台
  “流量分析 → 配额管理”为准，不能写死在项目中。
- [Open-Meteo 使用条款](https://open-meteo.com/en/terms)：免费接口为每分钟 600、每小时
  5000、每天 10000、每月 30 万，且限非商业使用。
- [12306 MCP](https://github.com/drfccv/mcp-server-12306)：当前为 Beta、非官方只读查询能力，
  没有公开服务等级或 QPS 保证。

## 基线结果

机器配置、Docker 版本和每轮 `summary.md` 必须一起记录。10 用户冒烟要求无未处理异常；
50、200、500 用户阶段允许暴露失败，但必须完整生成报告。这里不设置脱离机器配置的绝对
吞吐门槛，后续每个并发机制 PR 都使用同一组场景做前后对比。

### 2026-08-17 首次本地基线

测试机器：Intel Core i7-10870H，8 核 16 线程，15.8 GiB 内存，Docker 29.4.0。

| 用户数 | 阶段请求 | 平均 RPS | 峰值 RPS | p50 | p95 | p99 | HTTP 失败 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 950 | 26.94 | 33.60 | 24 ms | 140 ms | 180 ms | 0 |
| 50 | 2827 | 45.91 | 53.90 | 890 ms | 1.3 s | 1.6 s | 0 |
| 200 | 3594 | 40.64 | 45.70 | 4.4 s | 6.4 s | 6.7 s | 0 |
| 500 | 4177 | 34.88 | 40.80 | 15 s | 24 s | 25 s | 0 |

全程 11,548 个 HTTP 请求，未处理异常、SQLite locked 和重复行程均为 0。1843 个实际规划
会话只产生 21 次 Fake Provider 调用；其余同类请求由 TTL 缓存或进程内请求合并吸收。
铁路调用高于首次冷启动值，是因为五分钟测试跨过了两分钟车票缓存 TTL，属于预期刷新。

基线结论：系统在 50 用户左右已经接近吞吐拐点。并发从 50 增至 500 时 RPS 没有继续上升，
p95 从 1.3 秒扩大到 24 秒；Fake Provider 单次延迟仅 20～50 ms，LLM 实际只调用 2 次，
且 SQLite 没有锁错误。因此当前首要瓶颈不是外部供应商或内存，而是单 Worker 内同步本地
读写与请求排队形成的组合瓶颈。下一 PR 应先加入匿名访客隔离和 PostgreSQL 共享状态，再用
同一场景复测；在得到新的对照数据前，不提前加入 Redis 或多 Worker。

### 2026-08-17 故障场景冒烟

以下场景均使用 10 用户运行 8 秒。这里的目标不是比较吞吐，而是确认上游异常有明确边界，
不会演变为未处理异常、SQLite 锁错误、半成品行程或重复行程。

| 场景 | 请求 | HTTP 失败 | p95 | 未处理异常 | SQLite locked | 行程 | 重复行程 | 结果 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `slowllm` | 196 | 20 | 570 ms | 0 | 0 | 0 | 0 | 20 个意图请求按约定返回 504，其他步骤继续工作 |
| `raillimit` | 243 | 0 | 190 ms | 0 | 0 | 0 | 0 | 铁路 Fake 调用 54 次均返回 429，会话降级到等待选择 |
| `amaptimeout` | 303 | 0 | 150 ms | 0 | 0 | 0 | 0 | 高德地理编码超时后规划会话进入失败或取消状态 |
| `mixedfailure` | 256 | 0 | 220 ms | 0 | 0 | 0 | 0 | 铁路 429 与高德 503 被隔离，多数会话仍完成数据发现 |

`slowllm` 的 HTTP 失败属于 Locust 对预期 504 的如实计数，不是进程崩溃。四种场景均未保存
半成品行程，也没有发现数据库锁竞争或后台未处理异常。

### 2026-08-17 PostgreSQL 地点查询切换回归

为保持 Fake Upstream 压测网络完全隔离，本轮 Docker 场景显式设置
`CATALOG_SQLITE_ROLLBACK=true`。因此下表用于确认 API 并发行为没有回退，不把它误写成
PostgreSQL 吞吐提升结果：

| 用户数 | 阶段请求 | 平均 RPS | p50 | p95 | p99 | HTTP 失败 |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 1024 | 30.43 | 29 ms | 120 ms | 160 ms | 0 |
| 50 | 2853 | 46.34 | 800 ms | 1.2 s | 1.4 s | 0 |
| 200 | 3892 | 43.88 | 4.1 s | 6.4 s | 6.8 s | 0 |
| 500 | 4419 | 36.64 | 8 s | 20 s | 20 s | 0 |

全程 12,188 个请求，未处理异常、SQLite locked 和重复行程均为 0。结果仍显示单 Worker
在 50 用户后进入排队区间，符合本阶段“不提前修复业务存储瓶颈”的边界。

真实 `catalog` Schema 使用只读 `travelapp` 账号单独测量。一次查询包含“城市别名解析 +
80 公里三类 POI”，顺序执行 p50 约 109 ms、p95 约 133 ms；20 线程、8 个数据库连接池
并发执行 100 次时 p50 约 470 ms、p95 约 565 ms。执行计划确认城市查询命中
`locationnameexactidx`，附近查询命中 `locationpointidx`。该结果将作为后续业务 PostgreSQL
迁移后的数据库侧对照，不与隔离 Locust 数值混算。
