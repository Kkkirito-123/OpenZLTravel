"""OpenZLTravel 的集中运行配置。

该模块只负责把环境变量转换为受约束的不可变配置，不在导入时创建网络客户端，
也不会读取或输出密钥内容。图、Provider、认证和 Web 层都从这里取得统一配置，
避免每个模块自行解释环境变量。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, cast


class ConfigurationError(RuntimeError):
    """配置不安全或不完整时抛出的稳定启动错误。"""


Environment = Literal["development", "test", "production"]
AuthMode = Literal["dev", "signed"]
ProviderMode = Literal["fake", "live"]
RailProviderMode = Literal["public", "mcp"]


@dataclass(frozen=True, slots=True)
class Settings:
    """应用运行设置；实例创建后不可变，便于图和测试安全共享。"""

    environment: Environment
    auth_mode: AuthMode
    auth_secret: str | None
    cookie_name: str
    cookie_ttl_seconds: int
    cookie_secure: bool
    dev_user_id: str
    model_api_key: str | None
    model_base_url: str | None
    fast_model: str
    model_timeout_seconds: float
    provider_mode: ProviderMode
    rail_provider: RailProviderMode
    catalog_database_url: str | None
    amap_api_key: str | None
    amap_base_url: str
    amap_timeout_seconds: float
    allow_amap_fallback: bool
    open_meteo_base_url: str
    open_meteo_timeout_seconds: float
    rail_mcp_url: str
    rail_mcp_token: str | None
    rail_mcp_timeout_seconds: float
    rollinggo_mcp_url: str
    rollinggo_api_key: str | None
    rollinggo_timeout_seconds: float
    assistant_session_ttl_seconds: int
    travel_order_ttl_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        """读取环境变量并拒绝生产环境中的开发身份模式。"""

        environment = _choice(
            "APP_ENV", "development", {"development", "test", "production"}
        )
        auth_mode = _choice("AUTH_MODE", "dev", {"dev", "signed"})
        provider_mode = _choice("PROVIDER_MODE", "fake", {"fake", "live"})
        rail_provider = _choice("RAIL_PROVIDER", "public", {"public", "mcp"})
        secret = os.getenv("AUTH_SECRET") or None
        catalog_database_url = _first_env("CATALOG_DATABASE_URL")

        if auth_mode == "dev" and environment == "production":
            raise ConfigurationError("生产环境禁止使用 AUTH_MODE=dev")
        if auth_mode == "signed" and (secret is None or len(secret) < 32):
            raise ConfigurationError("AUTH_MODE=signed 要求至少 32 字符的 AUTH_SECRET")
        if rail_provider == "mcp" and not _first_env("RAIL_MCP_URL"):
            raise ConfigurationError("RAIL_PROVIDER=mcp 要求 RAIL_MCP_URL")

        return cls(
            environment=cast(Environment, environment),
            auth_mode=cast(AuthMode, auth_mode),
            auth_secret=secret,
            cookie_name=os.getenv("AUTH_COOKIE_NAME", "openzltravel_identity"),
            cookie_ttl_seconds=_positive_int("AUTH_COOKIE_TTL_SECONDS", 30 * 24 * 3600),
            cookie_secure=_boolean("AUTH_COOKIE_SECURE", environment == "production"),
            dev_user_id=os.getenv("DEV_USER_ID", "dev-local"),
            model_api_key=_first_env("OPENAI_API_KEY"),
            model_base_url=_first_env("OPENAI_BASE_URL"),
            fast_model=_first_env("TRAVEL_FAST_MODEL") or "gpt-5-mini",
            model_timeout_seconds=_positive_float("OPENAI_TIMEOUT_SECONDS", 20.0),
            provider_mode=cast(ProviderMode, provider_mode),
            rail_provider=cast(RailProviderMode, rail_provider),
            catalog_database_url=catalog_database_url,
            amap_api_key=os.getenv("AMAP_API_KEY") or None,
            amap_base_url=os.getenv("AMAP_BASE_URL", "https://restapi.amap.com/v3"),
            amap_timeout_seconds=_positive_float("AMAP_TIMEOUT_SECONDS", 10.0),
            allow_amap_fallback=_boolean("ALLOW_AMAP_FALLBACK", True),
            open_meteo_base_url=os.getenv(
                "OPEN_METEO_BASE_URL", "https://api.open-meteo.com/v1/forecast"
            ),
            open_meteo_timeout_seconds=_positive_float("OPEN_METEO_TIMEOUT_SECONDS", 8.0),
            rail_mcp_url=os.getenv("RAIL_MCP_URL", "http://127.0.0.1:8001/mcp").strip(),
            rail_mcp_token=os.getenv("RAIL_MCP_TOKEN") or None,
            rail_mcp_timeout_seconds=_positive_float("RAIL_MCP_TIMEOUT_SECONDS", 12.0),
            rollinggo_mcp_url=os.getenv(
                "ROLLINGGO_MCP_URL", "https://mcp.rollinggo.cn/mcp"
            ).strip(),
            rollinggo_api_key=_first_env("ROLLINGGO_API_KEY"),
            rollinggo_timeout_seconds=_positive_float("ROLLINGGO_TIMEOUT_SECONDS", 12.0),
            assistant_session_ttl_seconds=_positive_int(
                "ASSISTANT_SESSION_TTL_SECONDS", 12 * 3600
            ),
            travel_order_ttl_seconds=_positive_int("TRAVEL_ORDER_TTL_SECONDS", 10 * 60),
        )

    @property
    def signing_secret(self) -> str:
        """返回 Cookie 签名密钥；开发默认值只允许配合本机身份模式使用。"""

        if self.auth_secret:
            return self.auth_secret
        return "openzltravel-local-development-secret-only"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程级配置；测试可清理缓存后重新加载环境变量。"""

    return Settings.from_env()


def _choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ConfigurationError(f"{name} 必须是以下值之一：{choices}")
    return value


def _first_env(*names: str) -> str | None:
    """按优先级读取第一个非空环境变量，不记录值也不打印密钥。"""

    return next(
        (os.getenv(name, "").strip() for name in names if os.getenv(name, "").strip()),
        None,
    )


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} 必须是正整数") from error
    if value <= 0:
        raise ConfigurationError(f"{name} 必须是正整数")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} 必须是布尔值")


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} 必须是正数") from error
    if value <= 0:
        raise ConfigurationError(f"{name} 必须是正数")
    return value
