"""TravelGraph 可预期的稳定错误。"""


class TravelGraphError(Exception):
    """带稳定错误码的图异常基类。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ResumeValidationError(TravelGraphError):
    """interrupt 恢复载荷的类型或字段无效。"""


class FactBoundaryError(TravelGraphError):
    """Agent 输出引用了不存在的事实 ID。"""


class ModelUnavailableError(TravelGraphError):
    """模型未配置或上游暂时不可用。"""
