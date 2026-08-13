"""应用配置。

配置只从环境变量读取，密钥不进入代码、测试样例或日志。
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    """运行时配置。"""

    amap_api_key: str = os.getenv("AMAP_API_KEY", "")
    amap_base_url: str = os.getenv("AMAP_BASE_URL", "https://restapi.amap.com/v3")
    amap_timeout_seconds: float = float(os.getenv("AMAP_TIMEOUT_SECONDS", "20"))
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_model: str = os.getenv("LLM_MODEL", "")
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    database_path: str = os.getenv(
        "DATABASE_PATH", str(BACKEND_ROOT / "db" / "openzltravel.sqlite3")
    )
    amap_js_key: str = os.getenv("VITE_AMAP_JS_KEY", "")
    catalog_path: str = os.getenv(
        "CATALOG_PATH", str(BACKEND_ROOT / "data" / "catalog.sqlite3")
    )
    allow_amap_fallback: bool = os.getenv("ALLOW_AMAP_FALLBACK", "true").lower() == "true"
