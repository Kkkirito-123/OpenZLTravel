"""LangGraph 工作流层。

这里集中放置 State、Edge、Node、interrupt 和 Checkpoint。外部 HTTP 接口不放在
本目录，纯业务模型也不放在本目录；这样阅读图代码时可以只关注“状态如何流动”。

包入口刻意不导入 ``workflow``：导入 ``travel_graph.state`` 时不应顺带装配整张图、
Provider。需要构建图的调用方请显式使用 ``travel_graph.workflow``，这样模块依赖和
Agent Server 的组合根都能在导入语句中看清楚。
"""
