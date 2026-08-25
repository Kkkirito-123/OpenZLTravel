"""OpenZLTravel 后端根包。

目录按运行单元和依赖方向组织：``assistant`` 是 LangChain 对话服务，
``travel_graph`` 是 LangGraph 规划服务；``domain``、``infrastructure`` 和
``runtime`` 是二者共享的领域、外部适配与运行时边界。
"""
