"""OpenZLTravel 的稳定业务错误。

错误携带稳定 code 和用户可读 message，API 层不需要重复编写异常映射逻辑。
"""


class AppError(Exception):
    """可以安全返回给前端的业务错误。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ProviderError(AppError):
    """外部地图或模型服务不可用。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status_code=503)


class DraftError(AppError):
    """模型草稿无法解析或违反候选数据约束。"""

    def __init__(self, message: str) -> None:
        super().__init__("invalid_plan", message, status_code=502)


class NotFoundError(AppError):
    """请求的行程不存在。"""

    def __init__(self, message: str = "行程不存在") -> None:
        super().__init__("trip_not_found", message, status_code=404)


class ConflictError(AppError):
    """客户端基于过期版本编辑资源。"""

    def __init__(self, message: str = "行程已经更新，请刷新后重试") -> None:
        super().__init__("revision_conflict", message, status_code=409)
