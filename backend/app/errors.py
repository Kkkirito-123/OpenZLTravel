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


class CatalogUnavailableError(AppError):
    """公共地点数据库不可用，禁止自动放大为外部地图流量。"""

    def __init__(self, message: str = "公共地点数据库暂时不可用") -> None:
        super().__init__("catalog_unavailable", message, status_code=503)


class DatabaseUnavailableError(AppError):
    """业务 PostgreSQL 不可用。"""

    def __init__(self, message: str = "业务数据库暂时不可用") -> None:
        super().__init__("database_unavailable", message, status_code=503)


class CoordinationUnavailableError(AppError):
    """Redis 协调能力不可用，不能安全执行需要跨 Worker 互斥的操作。"""

    def __init__(self, message: str = "协调服务暂时不可用") -> None:
        super().__init__("coordination_unavailable", message, status_code=503)


class SessionBusyError(AppError):
    """同一会话正在由另一个请求修改。"""

    def __init__(self, message: str = "当前会话正在处理中，请稍后重试") -> None:
        super().__init__("session_busy", message, status_code=503)


class RateLimitExceededError(ProviderError):
    """全局 Provider 并发槽已满。"""

    def __init__(self, message: str = "外部服务当前繁忙，请稍后重试") -> None:
        super().__init__("rate_limit_exceeded", message)


class ResourceNotFoundError(AppError):
    """资源不存在，或不属于当前匿名访客。"""

    def __init__(self, message: str = "请求的资源不存在") -> None:
        super().__init__("resource_not_found", message, status_code=404)


class VisitorClaimError(AppError):
    """旧数据认领失败。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(code, message, status_code=status_code)


class DraftError(AppError):
    """模型草稿无法解析或违反候选数据约束。"""

    def __init__(self, message: str) -> None:
        super().__init__("invalid_plan", message, status_code=502)


class ConflictError(AppError):
    """客户端基于过期版本编辑资源。"""

    def __init__(self, message: str = "行程已经更新，请刷新后重试") -> None:
        super().__init__("revision_conflict", message, status_code=409)
