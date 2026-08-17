"""压测专用的可控上游服务。

本服务模拟 OpenZLTravel 已使用的最小真实协议，并记录供应商调用统计。它只用于
Docker Compose 压测环境，不读取真实密钥，也不会访问任何外部网络。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

ScenarioName = Literal["normal", "slowllm", "raillimit", "amaptimeout", "mixedfailure"]
SCENARIOS: tuple[ScenarioName, ...] = (
    "normal",
    "slowllm",
    "raillimit",
    "amaptimeout",
    "mixedfailure",
)


@dataclass(frozen=True, slots=True)
class RequestDecision:
    """一次模拟调用的延迟与失败决策。"""

    delay_seconds: float
    status_code: int | None = None


class ScenarioRequest(BaseModel):
    """仅限压测环境使用的场景切换请求。"""

    scenario: ScenarioName


class FakeStats:
    """线程安全地记录调用量、错误和总耗时。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self.reset()

    def reset(self) -> None:
        """清空全部计数，便于每轮实验独立统计。"""

        with getattr(self, "_lock", Lock()):
            self.started_at = time.time()
            self.calls: Counter[str] = Counter()
            self.failures: Counter[str] = Counter()
            self.rate_limits: Counter[str] = Counter()
            self.duration_ms: Counter[str] = Counter()
            self.operations: dict[str, Counter[str]] = defaultdict(Counter)

    def reserve(self, provider: str, operation: str) -> int:
        """登记调用并返回该供应商当前序号，用于确定性混合故障。"""

        with self._lock:
            self.calls[provider] += 1
            self.operations[provider][operation] += 1
            return self.calls[provider]

    def complete(self, provider: str, duration_ms: int, status_code: int) -> None:
        """登记调用终态；429 单独计数，便于观察限流。"""

        with self._lock:
            self.duration_ms[provider] += duration_ms
            if status_code >= 400:
                self.failures[provider] += 1
            if status_code == 429:
                self.rate_limits[provider] += 1

    def snapshot(self, scenario: str) -> dict[str, Any]:
        """返回不包含请求正文和密钥的安全统计。"""

        with self._lock:
            providers = sorted(self.calls)
            return {
                "scenario": scenario,
                "uptime_seconds": round(time.time() - self.started_at, 3),
                "providers": {
                    provider: {
                        "calls": self.calls[provider],
                        "failures": self.failures[provider],
                        "rate_limits": self.rate_limits[provider],
                        "average_duration_ms": round(
                            self.duration_ms[provider] / max(1, self.calls[provider]), 2
                        ),
                        "operations": dict(self.operations[provider]),
                    }
                    for provider in providers
                },
            }


app = FastAPI(title="OpenZLTravel Fake Upstream", version="1.0")
STATS = FakeStats()
_configured_scenario = os.getenv("FAKE_SCENARIO", "normal").lower()
CURRENT_SCENARIO: ScenarioName = (
    _configured_scenario if _configured_scenario in SCENARIOS else "normal"
)  # type: ignore[assignment]


@app.get("/health")
async def health() -> dict[str, str]:
    """返回 Fake 服务存活状态。"""

    return {"status": "ok", "scenario": CURRENT_SCENARIO}


@app.get("/stats")
async def stats() -> dict[str, Any]:
    """返回本轮上游调用统计。"""

    return STATS.snapshot(CURRENT_SCENARIO)


@app.post("/reset")
async def reset() -> dict[str, str]:
    """清空统计但保留当前场景。"""

    STATS.reset()
    return {"status": "reset", "scenario": CURRENT_SCENARIO}


@app.post("/scenario")
async def set_scenario(request: ScenarioRequest) -> dict[str, str]:
    """测试时切换故障场景；生产应用不会暴露此接口。"""

    global CURRENT_SCENARIO
    CURRENT_SCENARIO = request.scenario
    STATS.reset()
    return {"status": "updated", "scenario": CURRENT_SCENARIO}


@app.post("/rail/mcp")
async def rail_mcp(request: Request) -> Response:
    """模拟 12306 MCP 的 Streamable HTTP 生命周期。"""

    payload = await request.json()
    method = str(payload.get("method", "unknown"))
    if method == "initialize":
        return JSONResponse(
            _mcp_initialize(payload, "fake-rail"),
            headers={"Mcp-Session-Id": f"rail-{uuid4()}"},
        )
    if method == "notifications/initialized":
        return Response(status_code=status.HTTP_202_ACCEPTED)
    tool = str(payload.get("params", {}).get("name", "unknown"))
    decision, started = await _before("rail", tool)
    if decision.status_code is not None:
        return _failure("rail", started, decision.status_code)
    result = _rail_result(tool, payload.get("params", {}).get("arguments", {}))
    return _mcp_success("rail", started, payload, result)


@app.delete("/rail/mcp", status_code=status.HTTP_204_NO_CONTENT)
async def close_rail_session() -> Response:
    """接受客户端关闭 MCP 会话。"""

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/hotel/mcp")
async def hotel_mcp(request: Request) -> Response:
    """模拟 DIDA MCP 的酒店搜索和详情工具。"""

    payload = await request.json()
    method = str(payload.get("method", "unknown"))
    if method == "initialize":
        return JSONResponse(
            _mcp_initialize(payload, "fake-hotel"),
            headers={"Mcp-Session-Id": f"hotel-{uuid4()}"},
        )
    if method == "notifications/initialized":
        return Response(status_code=status.HTTP_202_ACCEPTED)
    tool = str(payload.get("params", {}).get("name", "unknown"))
    decision, started = await _before("hotel", tool)
    if decision.status_code is not None:
        return _failure("hotel", started, decision.status_code)
    result = _hotel_result(tool)
    return _mcp_success("hotel", started, payload, result)


@app.delete("/hotel/mcp", status_code=status.HTTP_204_NO_CONTENT)
async def close_hotel_session() -> Response:
    """接受客户端关闭酒店 MCP 会话。"""

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/hotel/hotelsearch")
async def rollinggo_search() -> Response:
    """兼容 RollingGo OAuth 客户端的酒店搜索协议。"""

    decision, started = await _before("hotel", "searchHotels")
    if decision.status_code is not None:
        return _failure("hotel", started, decision.status_code)
    return _json_success("hotel", started, _hotel_result("searchHotels"))


@app.post("/hotel/hoteldetail")
async def rollinggo_detail() -> Response:
    """兼容 RollingGo OAuth 客户端的酒店详情协议。"""

    decision, started = await _before("hotel", "getHotelDetail")
    if decision.status_code is not None:
        return _failure("hotel", started, decision.status_code)
    return _json_success("hotel", started, _hotel_result("getHotelDetail"))


@app.get("/amap/v3/geocode/geo")
async def amap_geocode(address: str = "杭州") -> Response:
    """返回高德地理编码格式。"""

    return await _amap_response(
        "geocode",
        {
            "status": "1",
            "geocodes": [
                {
                    "formatted_address": address,
                    "city": f"{address.rstrip('市')}市",
                    "district": "西湖区",
                    "adcode": "330100",
                    "location": "120.1551,30.2741",
                }
            ],
        },
    )


@app.get("/amap/v3/place/text")
async def amap_places(keywords: str = "风景名胜") -> Response:
    """按高德 POI 搜索格式返回景点、餐厅或酒店候选。"""

    category = {"餐饮服务": "餐厅", "住宿服务": "酒店"}.get(keywords, "景点")
    pois = [
        {
            "id": f"fake-{category}-{index}",
            "name": f"测试{category}{index}",
            "address": f"测试路 {index} 号",
            "location": f"{120.1500 + index * 0.01:.4f},{30.2700 + index * 0.008:.4f}",
            "type": keywords,
            "photos": [{"url": f"https://example.test/{category}/{index}.jpg"}],
        }
        for index in range(1, 7)
    ]
    return await _amap_response("place", {"status": "1", "pois": pois})


@app.get("/amap/v3/weather/weatherInfo")
async def amap_weather() -> Response:
    """返回七天高德天气兜底数据。"""

    today = date.today()
    casts = [
        {
            "date": (today + timedelta(days=offset)).isoformat(),
            "dayweather": "多云",
            "nightweather": "晴",
            "daytemp": "28",
            "nighttemp": "20",
        }
        for offset in range(10)
    ]
    return await _amap_response(
        "weather", {"status": "1", "forecasts": [{"casts": casts}]}
    )


@app.get("/amap/v3/direction/driving")
async def amap_driving() -> Response:
    """返回可绘制的高德驾车轨迹。"""

    return await _amap_response(
        "driving",
        {
            "status": "1",
            "route": {
                "paths": [
                    {
                        "distance": "5200",
                        "duration": "1200",
                        "steps": [{"polyline": "120.15,30.27;120.16,30.28"}],
                    }
                ]
            },
        },
    )


@app.get("/amap/v3/direction/transit/integrated")
async def amap_transit() -> Response:
    """返回含线路和轨迹的高德公交方案。"""

    return await _amap_response(
        "transit",
        {
            "status": "1",
            "route": {
                "transits": [
                    {
                        "distance": "6300",
                        "duration": "1800",
                        "segments": [
                            {
                                "walking": {"polyline": "120.15,30.27;120.151,30.271"},
                                "bus": {
                                    "buslines": [
                                        {
                                            "name": "地铁 1 号线",
                                            "type": "地铁",
                                            "departure_stop": {"name": "测试站 A"},
                                            "arrival_stop": {"name": "测试站 B"},
                                            "via_stops": [{"name": "测试中间站"}],
                                            "polyline": "120.151,30.271;120.16,30.28",
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ]
            },
        },
    )


@app.get("/open-meteo/v1/forecast")
async def open_meteo(start_date: str, end_date: str) -> Response:
    """返回 Open-Meteo daily 格式的连续天气。"""

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    days = [
        date.fromordinal(value)
        for value in range(start.toordinal(), end.toordinal() + 1)
    ]
    payload = {
        "daily": {
            "time": [item.isoformat() for item in days],
            "weather_code": [1] * len(days),
            "temperature_2m_max": [28] * len(days),
            "temperature_2m_min": [20] * len(days),
        }
    }
    decision, started = await _before("openmeteo", "forecast")
    if decision.status_code is not None:
        return _failure("openmeteo", started, decision.status_code)
    return _json_success("openmeteo", started, payload)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """模拟意图识别、旧规划和可选文案增强的 OpenAI Chat Completions。"""

    body = await request.json()
    decision, started = await _before("llm", "chat.completions")
    if decision.status_code is not None:
        return _failure("llm", started, decision.status_code)
    content = _llm_content(body)
    payload = {
        "id": f"fake-{uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "fake-model"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }
    return _json_success("llm", started, payload)


async def _amap_response(operation: str, payload: dict[str, Any]) -> Response:
    decision, started = await _before("amap", operation)
    if decision.status_code is not None:
        return _failure("amap", started, decision.status_code)
    return _json_success("amap", started, payload)


async def _before(provider: str, operation: str) -> tuple[RequestDecision, float]:
    """先登记调用再应用延迟，超时请求也会出现在统计中。"""

    sequence = STATS.reserve(provider, operation)
    decision = scenario_decision(CURRENT_SCENARIO, provider, sequence)
    started = time.perf_counter()
    if decision.delay_seconds:
        await asyncio.sleep(decision.delay_seconds)
    return decision, started


def scenario_decision(scenario: ScenarioName, provider: str, sequence: int) -> RequestDecision:
    """根据场景和调用序号产生可重复的延迟、429或 5xx。"""

    base = float(os.getenv("FAKE_BASE_DELAY_SECONDS", "0.02"))
    if scenario == "slowllm" and provider == "llm":
        return RequestDecision(float(os.getenv("FAKE_SLOW_LLM_SECONDS", "0.25")))
    if scenario == "raillimit" and provider == "rail":
        return RequestDecision(base, 429)
    if scenario == "amaptimeout" and provider == "amap":
        return RequestDecision(float(os.getenv("FAKE_TIMEOUT_DELAY_SECONDS", "0.2")))
    if scenario == "mixedfailure":
        failure_every = {"rail": 3, "hotel": 4, "openmeteo": 5, "amap": 4, "llm": 6}
        every = failure_every.get(provider)
        if every and sequence % every == 0:
            return RequestDecision(base, 429 if provider == "rail" else 503)
    delays = {"rail": 0.03, "hotel": 0.04, "amap": 0.02, "openmeteo": 0.02, "llm": 0.05}
    delay = os.getenv(
        f"FAKE_{provider.upper()}_DELAY_SECONDS",
        str(delays.get(provider, base)),
    )
    return RequestDecision(float(delay))


def _mcp_initialize(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": payload.get("id"),
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": name, "version": "1.0"},
        },
    }


def _mcp_success(
    provider: str, started: float, request: dict[str, Any], result: Any
) -> Response:
    payload = {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "result": {"structuredContent": result},
    }
    return _json_success(provider, started, payload)


def _json_success(provider: str, started: float, payload: Any) -> Response:
    STATS.complete(provider, _elapsed_ms(started), 200)
    return JSONResponse(payload)


def _failure(provider: str, started: float, status_code: int) -> Response:
    STATS.complete(provider, _elapsed_ms(started), status_code)
    return JSONResponse(
        {"error": {"code": f"fake_{provider}_failure", "message": "模拟上游故障"}},
        status_code=status_code,
    )


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _rail_result(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool == "search-stations":
        query = str(arguments.get("query", "测试站"))
        return {"success": True, "stations": [{"name": query, "code": "TST"}]}
    if tool == "query-tickets":
        return {
            "success": True,
            "trains": [
                {
                    "train_code": "G100",
                    "from_station": "测试站 A",
                    "to_station": "测试站 B",
                    "start_time": "08:00",
                    "arrive_time": "10:30",
                    "duration": "02:30",
                    "seats": {"second_class": "有", "first_class": "5"},
                }
            ],
        }
    if tool == "query-ticket-price":
        return {
            "data": [
                {
                    "train_code": str(arguments.get("train_code", "G100")),
                    "prices": {"二等座": "553.0", "一等座": "933.0"},
                }
            ]
        }
    if tool == "query-transfer":
        return {
            "transfers": [
                {
                    "middle_station": "测试中转站",
                    "total_duration": "04:30",
                    "segments": [
                        {
                            "train_code": "G101",
                            "from_station": "测试站 A",
                            "to_station": "测试中转站",
                            "start_time": "08:00",
                            "arrive_time": "10:00",
                            "duration": "02:00",
                            "seats": {"second_class": "有"},
                        },
                        {
                            "train_code": "D202",
                            "from_station": "测试中转站",
                            "to_station": "测试站 B",
                            "start_time": "10:30",
                            "arrive_time": "12:30",
                            "duration": "02:00",
                            "seats": {"second_class": "有"},
                        },
                    ],
                }
            ]
        }
    return {"success": True}


def _hotel_result(tool: str) -> dict[str, Any]:
    if tool == "getHotelDetail":
        return {
            "success": True,
            "hotelId": 1001,
            "name": "测试湖景酒店",
            "description": "压测环境酒店详情",
            "facilities": ["Wi-Fi", "停车场"],
            "images": ["https://example.test/hotel/detail.jpg"],
            "roomRatePlans": [
                {
                    "ratePlanId": "rate-1",
                    "roomNameCn": "湖景大床房",
                    "totalPrice": 428,
                    "inventoryCount": 3,
                    "cancellationPolicies": [{"description": "入住前一天可免费取消"}],
                }
            ],
        }
    return {
        "hotelInformationList": [
            {
                "hotelId": 1001,
                "name": "测试湖景酒店",
                "address": "测试湖滨路 1 号",
                "latitude": 30.275,
                "longitude": 120.16,
                "distanceInMeters": 650,
                "starRating": 4.5,
                "price": {"hasPrice": True, "lowestPrice": 428},
                "imageUrl": "https://example.test/hotel/1001.jpg",
                "bookingUrl": "https://example.test/booking/1001",
                "hotelAmenities": ["Wi-Fi", "停车场"],
            }
        ]
    }


def _llm_content(body: dict[str, Any]) -> str:
    messages = body.get("messages", [])
    system = "\n".join(
        str(item.get("content", ""))
        for item in messages
        if isinstance(item, dict) and item.get("role") == "system"
    )
    if "只输出摘要、每日主题" in system:
        user = next(
            (
                str(item.get("content", "{}"))
                for item in messages
                if isinstance(item, dict) and item.get("role") == "user"
            ),
            "{}",
        )
        theme_count = max(1, user.count('"theme') or user.count("主题"))
        return (
            '{"summary":"压测环境生成的旅行摘要","themes":['
            + ",".join(f'"第{index}天"' for index in range(1, theme_count + 1))
            + '],"tips":["出发前请复核实时信息。"]}'
        )
    if "travel dialogue command generator" in system:
        return '{"commands":[{"type":"route_to_chat"}]}'
    return '{"summary":"压测行程","days":[],"tips":[]}'
