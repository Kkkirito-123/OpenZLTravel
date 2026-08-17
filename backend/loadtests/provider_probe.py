"""真实供应商的只读容量探针。

探针默认拒绝运行，必须显式传入 ``--confirm-live``。它只做少量只读请求并记录延迟，
不用于寻找供应商极限，也不输出密钥、令牌或完整响应。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import httpx

from app.providers.base import McpHttpClient
from app.providers.hotels import RollingGoHotelClient

ProviderName = Literal["rail", "amap", "openmeteo", "hotel", "llm"]
PROVIDERS: tuple[ProviderName, ...] = ("rail", "amap", "openmeteo", "hotel", "llm")
MAX_CALLS = 6
Operation = tuple[
    Callable[[], Awaitable[None]] | None,
    Callable[[], Awaitable[None]] | None,
    str,
]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """一次安全探针的汇总，不保存供应商正文。"""

    provider: str
    status: str
    calls: int
    successes: int
    failures: int
    average_ms: float | None
    p95_ms: float | None
    note: str = ""


def validate_limits(provider: ProviderName, calls: int, concurrency: int) -> tuple[int, int]:
    """应用公开限制和项目保守上限，防止参数绕过安全边界。"""

    call_cap = 3 if provider == "openmeteo" else MAX_CALLS
    safe_calls = min(max(1, calls), call_cap)
    concurrency_cap = 1 if provider in {"rail", "openmeteo"} else 2
    if provider == "amap":
        account_qps = max(1, int(os.getenv("AMAP_ACCOUNT_QPS", "1")))
        concurrency_cap = min(2, account_qps)
    return safe_calls, min(max(1, concurrency), concurrency_cap)


async def run_probe(
    provider: ProviderName,
    calls: int,
    concurrency: int,
    confirm_live: bool,
) -> ProbeResult:
    """执行受硬上限保护的真实只读探针。"""

    if not confirm_live:
        raise ValueError("真实供应商探针必须显式使用 --confirm-live")
    safe_calls, safe_concurrency = validate_limits(provider, calls, concurrency)
    operation, closer, skip_note = _operation(provider)
    if operation is None:
        return ProbeResult(provider, "skipped", 0, 0, 0, None, None, skip_note)
    try:
        latencies = await _run_calls(operation, safe_calls, safe_concurrency)
    finally:
        if closer is not None:
            await closer()
    successes = sum(value >= 0 for value in latencies)
    valid = sorted(value for value in latencies if value >= 0)
    return ProbeResult(
        provider=provider,
        status="ok" if successes == safe_calls else "partial",
        calls=safe_calls,
        successes=successes,
        failures=safe_calls - successes,
        average_ms=round(sum(valid) / len(valid), 2) if valid else None,
        p95_ms=_percentile(valid, 0.95),
        note=f"并发上限 {safe_concurrency}，总调用硬上限 {call_cap(provider)}",
    )


def call_cap(provider: ProviderName) -> int:
    """返回文档化的探针调用硬上限。"""

    return 3 if provider == "openmeteo" else MAX_CALLS


async def _run_calls(
    operation: Callable[[], Awaitable[None]], calls: int, concurrency: int
) -> list[float]:
    semaphore = asyncio.Semaphore(concurrency)

    async def invoke() -> float:
        async with semaphore:
            started = time.perf_counter()
            try:
                await operation()
            except Exception:
                return -1
            return round((time.perf_counter() - started) * 1000, 2)

    return list(await asyncio.gather(*(invoke() for _ in range(calls))))


def _operation(
    provider: ProviderName,
) -> Operation:
    factories: dict[ProviderName, Callable[[], Operation]] = {
        "rail": _rail_operation,
        "amap": _amap_operation,
        "openmeteo": _openmeteo_operation,
        "hotel": _hotel_operation,
        "llm": _llm_operation,
    }
    return factories[provider]()


def _rail_operation() -> Operation:
    """构造 12306 车站查询探针。"""

    client = McpHttpClient(
        os.getenv("RAIL_MCP_URL", "http://127.0.0.1:8001/mcp"),
        float(os.getenv("RAIL_MCP_TIMEOUT_SECONDS", "12")),
    )

    async def rail() -> None:
        await client.call_tool("search-stations", {"query": "杭州", "limit": 3})

    return rail, client.aclose, ""


def _amap_operation() -> Operation:
    """构造高德地理编码探针。"""

    key = os.getenv("AMAP_API_KEY", "")
    if not key:
        return None, None, "未配置 AMAP_API_KEY"
    client = httpx.AsyncClient(
        base_url=os.getenv("AMAP_BASE_URL", "https://restapi.amap.com/v3"),
        timeout=float(os.getenv("AMAP_TIMEOUT_SECONDS", "20")),
    )

    async def amap() -> None:
        response = await client.get("/geocode/geo", params={"address": "杭州", "key": key})
        response.raise_for_status()

    return amap, client.aclose, ""


def _openmeteo_operation() -> Operation:
    """构造 Open-Meteo 单日预报探针。"""

    client = httpx.AsyncClient(timeout=10)
    base_url = os.getenv("OPEN_METEO_BASE_URL", "https://api.open-meteo.com/v1/forecast")

    async def weather() -> None:
        response = await client.get(
            base_url,
            params={
                "latitude": 30.2741,
                "longitude": 120.1551,
                "daily": "weather_code",
                "forecast_days": 1,
            },
        )
        response.raise_for_status()

    return weather, client.aclose, ""


def _hotel_operation() -> Operation:
    token_path = Path(
        os.getenv("ROLLINGGO_HOTEL_TOKEN_PATH", str(Path.home() / ".hotel-cli" / "token.json"))
    ).expanduser()
    if token_path.is_file():
        client: McpHttpClient | RollingGoHotelClient = RollingGoHotelClient(
            os.getenv("ROLLINGGO_HOTEL_BASE_URL", "https://mcp.rollinggo.cn/mcp"),
            str(token_path),
            float(os.getenv("ROLLINGGO_HOTEL_TIMEOUT_SECONDS", "12")),
        )
    elif os.getenv("DIDA_API_KEY"):
        client = McpHttpClient(
            os.getenv("DIDA_MCP_URL", "https://mcp.rollinggo.cn/mcp"),
            float(os.getenv("DIDA_MCP_TIMEOUT_SECONDS", "12")),
            os.environ["DIDA_API_KEY"],
        )
    else:
        return None, None, "酒店服务未登录且未配置 DIDA_API_KEY"

    async def hotel() -> None:
        await client.call_tool(
            "searchHotels",
            {
                "originQuery": "杭州酒店",
                "place": "杭州",
                "placeType": "城市",
                "checkInParam": {
                    "adultCount": 1,
                    "checkInDate": (date.today() + timedelta(days=14)).isoformat(),
                    "stayNights": 1,
                },
                "size": 3,
            },
        )

    return hotel, client.aclose, ""


def _llm_operation() -> Operation:
    if os.getenv("LLM_PROBE_ENABLED", "false").lower() != "true":
        return None, None, "LLM_PROBE_ENABLED 未显式启用"
    if not os.getenv("LLM_API_KEY") or not os.getenv("LLM_MODEL"):
        return None, None, "LLM 配置不完整"
    client = httpx.AsyncClient(
        base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
    )

    async def llm() -> None:
        response = await client.post(
            "/chat/completions",
            json={
                "model": os.environ["LLM_MODEL"],
                "messages": [{"role": "user", "content": "只回复：ok"}],
                "max_tokens": 8,
                "temperature": 0,
            },
        )
        response.raise_for_status()

    return llm, client.aclose, ""


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, round((len(values) - 1) * ratio)))
    return round(values[index], 2)


def main() -> int:
    """解析命令行并输出安全 JSON 报告。"""

    parser = argparse.ArgumentParser(description="OpenZLTravel 真实供应商只读容量探针")
    parser.add_argument("provider", choices=PROVIDERS)
    parser.add_argument("--calls", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(
            run_probe(args.provider, args.calls, args.concurrency, args.confirm_live)
        )
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
