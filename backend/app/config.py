"""应用配置。

配置只从环境变量读取，密钥不进入代码、测试样例或日志。
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_ROOT / ".env")
# 本地运行账号由 catalog.ps1 生成到独立文件，避免改写用户已有的模型和地图密钥配置。
load_dotenv(BACKEND_ROOT / ".env.runtime.local", override=False)


@dataclass(frozen=True)
class Settings:
    """运行时配置。"""

    amap_api_key: str = os.getenv("AMAP_API_KEY", "")
    amap_base_url: str = os.getenv("AMAP_BASE_URL", "https://restapi.amap.com/v3")
    amap_timeout_seconds: float = float(os.getenv("AMAP_TIMEOUT_SECONDS", "20"))
    amap_cache_path: str = os.getenv(
        "AMAP_CACHE_PATH", str(BACKEND_ROOT / "data" / "amap-cache.json")
    )
    amap_cache_ttl_seconds: int = int(os.getenv("AMAP_CACHE_TTL_SECONDS", "86400"))
    amap_min_interval_seconds: float = float(os.getenv("AMAP_MIN_INTERVAL_SECONDS", "0.4"))
    amap_rate_limit_cooldown_seconds: float = float(
        os.getenv("AMAP_RATE_LIMIT_COOLDOWN_SECONDS", "30")
    )
    amap_scheduler_concurrency: int = int(os.getenv("AMAP_SCHEDULER_CONCURRENCY", "2"))
    open_meteo_base_url: str = os.getenv(
        "OPEN_METEO_BASE_URL", "https://api.open-meteo.com/v1/forecast"
    )
    open_meteo_timeout_seconds: float = float(os.getenv("OPEN_METEO_TIMEOUT_SECONDS", "15"))
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_model: str = os.getenv("LLM_MODEL", "")
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    llm_enhancement_timeout_seconds: float = float(
        os.getenv("LLM_ENHANCEMENT_TIMEOUT_SECONDS", "8")
    )
    intent_llm_timeout_seconds: float = float(os.getenv("INTENT_LLM_TIMEOUT_SECONDS", "15"))
    intent_context_max_chars: int = int(os.getenv("INTENT_CONTEXT_MAX_CHARS", "5000"))
    intent_result_cache_ttl_seconds: int = int(os.getenv("INTENT_RESULT_CACHE_TTL_SECONDS", "3600"))
    # 只有确认模型服务支持 OpenAI prompt_cache_key 时才配置；空值保持兼容。
    intent_prompt_cache_key: str = os.getenv("INTENT_PROMPT_CACHE_KEY", "")
    conversation_recent_token_limit: int = int(os.getenv("CONVERSATION_RECENT_TOKEN_LIMIT", "3000"))
    conversation_summary_token_limit: int = int(
        os.getenv("CONVERSATION_SUMMARY_TOKEN_LIMIT", "800")
    )
    rail_mcp_url: str = os.getenv("RAIL_MCP_URL", "http://127.0.0.1:8001/mcp")
    rail_mcp_timeout_seconds: float = float(os.getenv("RAIL_MCP_TIMEOUT_SECONDS", "12"))
    rollinggo_hotel_base_url: str = os.getenv(
        "ROLLINGGO_HOTEL_BASE_URL", "https://mcp.rollinggo.cn/mcp"
    )
    rollinggo_hotel_token_path: str = os.getenv(
        "ROLLINGGO_HOTEL_TOKEN_PATH", str(Path.home() / ".hotel-cli" / "token.json")
    )
    rollinggo_hotel_timeout_seconds: float = float(
        os.getenv("ROLLINGGO_HOTEL_TIMEOUT_SECONDS", "12")
    )
    # 兼容早期供应商 Token 配置；新环境优先使用 RollingGo OAuth 登录。
    dida_mcp_url: str = os.getenv("DIDA_MCP_URL", "https://mcp.rollinggo.cn/mcp")
    dida_api_key: str = os.getenv("DIDA_API_KEY", "")
    dida_mcp_timeout_seconds: float = float(os.getenv("DIDA_MCP_TIMEOUT_SECONDS", "12"))
    provider_concurrency: int = int(os.getenv("PROVIDER_CONCURRENCY", "4"))
    provider_failure_threshold: int = int(os.getenv("PROVIDER_FAILURE_THRESHOLD", "3"))
    provider_cooldown_seconds: float = float(os.getenv("PROVIDER_COOLDOWN_SECONDS", "30"))
    redis_url: str = os.getenv("REDIS_URL", "")
    redis_timeout_seconds: float = float(os.getenv("REDIS_TIMEOUT_SECONDS", "2"))
    session_lock_ttl_seconds: int = int(os.getenv("SESSION_LOCK_TTL_SECONDS", "30"))
    task_lease_ttl_seconds: int = int(os.getenv("TASK_LEASE_TTL_SECONDS", "30"))
    task_lease_renew_seconds: int = int(os.getenv("TASK_LEASE_RENEW_SECONDS", "10"))
    recovery_scan_seconds: int = int(os.getenv("RECOVERY_SCAN_SECONDS", "5"))
    amap_provider_concurrency: int = int(os.getenv("AMAP_PROVIDER_CONCURRENCY", "2"))
    rail_provider_concurrency: int = int(os.getenv("RAIL_PROVIDER_CONCURRENCY", "1"))
    hotel_provider_concurrency: int = int(os.getenv("HOTEL_PROVIDER_CONCURRENCY", "2"))
    llm_provider_concurrency: int = int(os.getenv("LLM_PROVIDER_CONCURRENCY", "4"))
    api_rate_limit_per_minute: int = int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "120"))
    database_url: str = os.getenv("DATABASE_URL", os.getenv("CATALOG_DATABASE_URL", ""))
    database_pool_min_size: int = int(os.getenv("DATABASE_POOL_MIN_SIZE", "2"))
    database_pool_max_size: int = int(os.getenv("DATABASE_POOL_MAX_SIZE", "20"))
    database_pool_timeout_seconds: float = float(
        os.getenv("DATABASE_POOL_TIMEOUT_SECONDS", "5")
    )
    conversation_pool_min_size: int = int(os.getenv("CONVERSATION_POOL_MIN_SIZE", "1"))
    conversation_pool_max_size: int = int(os.getenv("CONVERSATION_POOL_MAX_SIZE", "4"))
    visitor_cookie_secure: bool = os.getenv("VISITOR_COOKIE_SECURE", "false").lower() == "true"
    amap_js_key: str = os.getenv("VITE_AMAP_JS_KEY", "")
    catalog_database_url: str = os.getenv("CATALOG_DATABASE_URL", "")
    catalog_pool_min_size: int = int(os.getenv("CATALOG_POOL_MIN_SIZE", "1"))
    catalog_pool_max_size: int = int(os.getenv("CATALOG_POOL_MAX_SIZE", "4"))
    catalog_pool_timeout_seconds: float = float(os.getenv("CATALOG_POOL_TIMEOUT_SECONDS", "3"))
    allow_amap_fallback: bool = os.getenv("ALLOW_AMAP_FALLBACK", "true").lower() == "true"
