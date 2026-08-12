# OpenZLTravel 开发约定

## 项目定位

OpenZLTravel 是独立的轻量旅行规划 MVP，目前不依赖 `re_zlagent`，也不引入
第三方 Agent 框架。后续若接入 OpenZLAgent，必须通过明确接口集成，不得把旅行
业务代码放入 `src/re_zlagent/`。

## 阅读顺序与依赖方向

后端优先按 `main.py → travel.py → providers.py / storage.py → models.py` 阅读：

```text
main → travel → providers / storage / models / errors
providers → config / models / errors
storage → models
models → 无业务依赖
```

- `backend/app/main.py`：FastAPI 装配、路由、异常映射和依赖组装，不写业务规则。
- `backend/app/travel.py`：旅行主流程、事实校验、组装、预算和 Markdown 导出。
- `backend/app/providers.py`：高德与 OpenAI 兼容模型客户端，以及最小依赖协议。
- `backend/app/storage.py`：仓库协议和 SQLite 实现；保持现有 Schema 兼容。
- `backend/app/models.py`：前后端稳定数据结构，不依赖框架和基础设施。
- `backend/app/config.py`、`errors.py`：环境配置与稳定业务错误。
- `frontend/src/pages/`：创建、结果和历史三个页面；公共 API、类型和地图组件位于
  `frontend/src/` 根级。

## 业务与风格约束

- 模型只能引用高德候选池中的 POI ID；城市、地址、坐标、天气和路线不能由模型补写。
- 地图只能绘制高德返回的真实 `polyline`；轨迹缺失时显示提示，不得用 POI 直线冒充路线。
- 第三方 POI 图片只保存合法 HTTP(S) URL，不下载、代理或缓存，且不得进入模型提示词。
- 每日预算先按经验参数估算，再逐项汇总为全程预算；页面和导出必须标记为估算值。
- 外部查询与全部校验成功后才能写入 SQLite，失败流程不得留下半成品。
- 测试不访问真实高德或模型服务，统一使用 Fake 实现和临时数据库。
- Python 和普通 TypeScript 文件使用小写命名，Vue 组件使用 PascalCase，页面统一为
  `*Page.vue`。
- 公共 Python 类和函数使用中文 docstring；注释解释约束原因，不逐行翻译代码。
- 优先早返回与单一职责函数，普通函数尽量不超过 40 行，嵌套不超过两层。
- Ruff 启用 `C901`，圈复杂度上限为 8；不为未批准的未来功能添加抽象。
- 不读取、提交或输出真实密钥、`.env`、SQLite 数据库和构建产物。

## 验证命令

```powershell
cd backend
python -m pytest -q
python -m ruff check app tests
python -m mypy app

cd ..\frontend
npm.cmd test
npm.cmd run build
```
