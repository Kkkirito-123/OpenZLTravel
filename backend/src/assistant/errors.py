"""Assistant 模型调用与输出解析错误。"""


class AssistantModelError(RuntimeError):
    """LLM 未配置、调用失败或没有返回合法结果。"""
