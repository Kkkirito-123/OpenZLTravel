# OpenZLTravel

OpenZLTravel 是从零实现的轻量旅行规划 MVP。它通过受控流水线组合真实地图数据和
模型规划能力，不复制参考项目代码，也暂不依赖 OpenZLAgent 或第三方 Agent 框架。

## 当前能力

- 输入目的地、日期、人数、预算、节奏、住宿和旅行偏好。
- 使用高德 Web 服务查询城市、景点、餐厅、酒店、天气和路线。
- 使用 OpenAI 兼容模型从真实候选池中组织 1～7 天结构化行程。
- 程序校验所有模型引用的 POI，禁止模型伪造地图事实。
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
校验草稿、补全路线、估算预算，最后一次性保存完整行程。

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

## 设计边界

当前版本采用同步受控流水线。预算明确属于估算值；天气超出供应商覆盖范围时标记
“暂无预报”。第一版不实现登录、多城市、RAG、PDF、长期记忆、长任务恢复、
多 Agent 或 OpenZLAgent 接入。
