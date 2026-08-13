# OpenZLTravel

OpenZLTravel 是从零实现的轻量旅行规划 MVP。它通过受控流水线组合真实地图数据和
模型规划能力，不复制参考项目代码，也暂不依赖 OpenZLAgent 或第三方 Agent 框架。

## 当前能力

- 输入目的地、日期、人数、预算、节奏、住宿和旅行偏好。
- 使用高德 Web 服务查询城市、景点、餐厅、酒店、天气和路线。
- 使用 OpenAI 兼容模型从真实候选池中组织 1～7 天结构化行程。
- 程序校验所有模型引用的 POI，禁止模型伪造地图事实。
- 地图按天展示高德真实驾车轨迹、距离和预计时间，轨迹缺失时不会绘制虚假直线。
- 展示景点、餐厅和酒店的高德 POI 图片；没有图片或加载失败时自动使用占位区域。
- 提供每日和全程预算明细，所有金额均明确标记为经验参数估算值。
- 生成成功后自动保存到本地 SQLite，支持查看和删除历史行程。
- 支持 Markdown 导出和只读高德地图；未配置前端 Key 时给出明确提示。

## 项目结构

```text
backend/
  app/
    main.py          FastAPI 入口、路由、异常处理和依赖装配
    config.py        环境配置
    models.py        全部 Pydantic 数据模型
    errors.py        稳定业务错误
    providers.py     高德客户端、模型规划器和依赖协议
    storage.py       SQLite 行程仓库
    travel.py        行程编排、校验、预算和 Markdown 导出
  tests/             Fake 外部服务与离线测试
frontend/
  src/
    api.ts           后端 API 客户端
    types.ts         前端数据类型
    TripMap.vue      只读高德地图
    pages/
      PlanPage.vue   创建行程
      TripPage.vue   查看结果
      HistoryPage.vue 历史记录
```

核心调用关系：

```text
Vue 页面 → FastAPI main → TravelService
                         ├─ AmapClient / LlmPlanner
                         └─ SqliteTripRepository
```

`TravelService.create()` 保持线性流程：确认城市、获取候选和天气、让模型选择候选、
校验草稿、补全路线和图片、逐日估算并汇总预算，最后一次性保存完整行程。

预算使用固定、透明的 MVP 规则：按人数估算餐饮和门票，按住宿等级计算住宿，
按真实路线距离估算交通，其他费用在各天平均分配。用户填写的预算只用于超额提示，
不会反向修改预计花费。

## 本地启动

后端：

```powershell
cd backend
Copy-Item .env.example .env
# 编辑 backend/.env，填写高德与 OpenAI 兼容模型配置
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

前端：

```powershell
cd frontend
Copy-Item .env.example .env
npm.cmd install
npm.cmd run dev
```

浏览器访问 <http://127.0.0.1:5173>。也可以在项目根目录执行：

```powershell
.\start.ps1 -Install
```

启动脚本使用 `npm.cmd`，可避开 PowerShell 禁止执行 `npm.ps1` 的问题。

## 验证

```powershell
cd backend
python -m pytest -q
python -m ruff check app tests
python -m mypy app

cd ..\frontend
npm.cmd test
npm.cmd run build
```

离线测试使用 Fake 地图、Fake 模型和 SQLite 临时数据库，不访问真实服务。真实高德和
模型联调需要配置本地密钥，并应与离线质量门禁分开执行。

## 本地公开数据

项目支持使用已经下载并构建的全国本地目录，优先从 `backend/data/catalog.sqlite3` 读取城市和 POI，命中后不会调用高德城市与地点搜索接口。

原始数据位于 `backend/data/raw/`：全国 OpenStreetMap PBF 和 GeoNames 城市压缩包。它们体积较大且已加入 Git 忽略，不会进入提交；需要重新构建时，在 `backend` 目录执行：

```powershell
python scripts/build_catalog.py
```

本地目录只负责城市、景点、餐饮和酒店基础事实；天气与驾车路线继续使用高德，避免把过期数据或直线距离伪装成实时结果。`ALLOW_AMAP_FALLBACK=true` 时，未覆盖的城市仍可回退高德；设为 `false` 可完全禁止这类调用。

## 设计边界

当前版本采用同步受控流水线。天气超出供应商覆盖范围时标记“暂无预报”；第三方
图片只保存 URL，不下载或缓存。旧 SQLite 行程没有图片或每日预算字段时仍可读取。
当前不实现登录、多城市、RAG、PDF、长期记忆、长任务恢复、多 Agent 或
OpenZLAgent 接入。
