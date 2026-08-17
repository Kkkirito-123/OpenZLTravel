"""并发实验室的 Fake 协议与故障场景测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from loadtests.config import parse_stages, planning_request
from loadtests.fake_upstream import app, scenario_decision
from loadtests.provider_probe import validate_limits
from loadtests.report import write_report


def test_normal_scenario_supports_mcp_lifecycle_and_statistics() -> None:
    """正常场景必须兼容生产 MCP 客户端使用的初始化与工具调用。"""

    with TestClient(app) as client:
        client.post("/scenario", json={"scenario": "normal"})
        initialized = client.post(
            "/rail/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        notification = client.post(
            "/rail/mcp",
            headers={"Mcp-Session-Id": initialized.headers["Mcp-Session-Id"]},
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        result = client.post(
            "/rail/mcp",
            headers={"Mcp-Session-Id": initialized.headers["Mcp-Session-Id"]},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "query-tickets", "arguments": {}},
            },
        )

        assert initialized.json()["result"]["protocolVersion"] == "2025-06-18"
        assert notification.status_code == 202
        assert result.json()["result"]["structuredContent"]["trains"][0]["train_code"] == "G100"
        stats = client.get("/stats").json()
        assert stats["providers"]["rail"]["calls"] == 1
        assert stats["providers"]["rail"]["operations"] == {"query-tickets": 1}


def test_slow_llm_scenario_returns_valid_openai_response(monkeypatch) -> None:
    """慢模型只增加可控延迟，响应结构仍可被 OpenAI 客户端解析。"""

    monkeypatch.setenv("FAKE_SLOW_LLM_SECONDS", "0.001")
    with TestClient(app) as client:
        client.post("/scenario", json={"scenario": "slowllm"})
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "fake-model",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a travel dialogue command generator",
                    }
                ],
            },
        )

        content = response.json()["choices"][0]["message"]["content"]
        assert json.loads(content) == {"commands": [{"type": "route_to_chat"}]}
        assert response.json()["usage"]["total_tokens"] == 120


def test_rail_limit_scenario_returns_429_without_hiding_count() -> None:
    """12306 限流必须是 HTTP 429，并进入独立限流计数。"""

    with TestClient(app) as client:
        client.post("/scenario", json={"scenario": "raillimit"})
        response = client.post(
            "/rail/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "query-tickets", "arguments": {}},
            },
        )

        assert response.status_code == 429
        rail = client.get("/stats").json()["providers"]["rail"]
        assert rail["calls"] == rail["failures"] == rail["rate_limits"] == 1


def test_amap_timeout_scenario_uses_configurable_long_delay(monkeypatch) -> None:
    """高德超时场景的延迟可在单测和容器中分别缩放。"""

    monkeypatch.setenv("FAKE_TIMEOUT_DELAY_SECONDS", "0.321")
    decision = scenario_decision("amaptimeout", "amap", 1)

    assert decision.delay_seconds == 0.321
    assert decision.status_code is None


def test_mixed_failure_scenario_is_deterministic() -> None:
    """混合故障按固定序号注入，保证两轮基线可以直接比较。"""

    assert scenario_decision("mixedfailure", "rail", 3).status_code == 429
    assert scenario_decision("mixedfailure", "hotel", 4).status_code == 503
    assert scenario_decision("mixedfailure", "openmeteo", 5).status_code == 503
    assert scenario_decision("mixedfailure", "amap", 4).status_code == 503
    assert scenario_decision("mixedfailure", "llm", 6).status_code == 503
    assert scenario_decision("mixedfailure", "rail", 2).status_code is None


def test_fake_map_weather_and_hotel_contracts() -> None:
    """地图、天气与酒店响应保留生产解析器所需的关键字段。"""

    with TestClient(app) as client:
        client.post("/scenario", json={"scenario": "normal"})
        city = client.get("/amap/v3/geocode/geo", params={"address": "杭州"}).json()
        weather = client.get(
            "/open-meteo/v1/forecast",
            params={"start_date": "2026-08-18", "end_date": "2026-08-20"},
        ).json()
        hotel = client.post(
            "/hotel/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "searchHotels", "arguments": {}},
            },
        ).json()

        assert city["geocodes"][0]["adcode"] == "330100"
        assert len(weather["daily"]["time"]) == 3
        content = hotel["result"]["structuredContent"]
        assert content["hotelInformationList"][0]["hotelId"] == 1001


def test_locust_stages_and_planning_request_are_stable() -> None:
    """阶段配置必须可重复，规划请求始终满足现有 API 约束。"""

    assert parse_stages("10:30,50:60,200:90,500:120") == [
        (10, 30),
        (50, 60),
        (200, 90),
        (500, 120),
    ]
    request = planning_request()
    assert request["origin"] == "上海"
    assert request["destination"] == "杭州"
    assert request["transport_mode"] == "auto"


def test_live_probe_applies_hard_caps(monkeypatch) -> None:
    """真实探针不能通过命令行参数突破供应商安全上限。"""

    monkeypatch.setenv("AMAP_ACCOUNT_QPS", "8")
    assert validate_limits("rail", 100, 100) == (6, 1)
    assert validate_limits("openmeteo", 100, 100) == (3, 1)
    assert validate_limits("amap", 100, 100) == (6, 2)
    assert validate_limits("hotel", 100, 100) == (6, 2)


def test_report_combines_locust_fake_and_sqlite_metrics(tmp_path: Path) -> None:
    """报告必须同时包含 HTTP、Provider、缓存和重复结果指标。"""

    results = tmp_path / "results"
    results.mkdir()
    (results / "locust_stats.csv").write_text(
        "Type,Name,Request Count,Failure Count,Median Response Time,50%,95%,99%,Requests/s\n"
        ",Aggregated,100,2,20,20,80,120,10.5\n",
        encoding="utf-8",
    )
    (results / "locust_failures.csv").write_text(
        "Method,Name,Error,Occurrences\nGET,/health,database is locked,2\n",
        encoding="utf-8",
    )
    (results / "locust_stats_history.csv").write_text(
        "Timestamp,User Count,Type,Name,Requests/s,Failures/s,50%,95%,99%,"
        "Total Request Count,Total Failure Count\n"
        "1,10,,Aggregated,10,0,20,80,120,100,0\n"
        "2,50,,Aggregated,20,0,40,160,240,300,1\n",
        encoding="utf-8",
    )
    (results / "machine.json").write_text(
        json.dumps(
            {
                "cpu": "test-cpu",
                "logical_processors": 8,
                "memory_gb": 16,
                "docker_version": "29",
            }
        ),
        encoding="utf-8",
    )
    (results / "app.log").write_text(
        "ERROR: Exception in ASGI application\nsqlite3.OperationalError: database is locked\n",
        encoding="utf-8",
    )
    (results / "fake-stats.json").write_text(
        json.dumps(
            {
                "scenario": "normal",
                "providers": {"rail": {"calls": 4}, "llm": {"calls": 1}},
            }
        ),
        encoding="utf-8",
    )
    (results / "business-counters.json").write_text(
        json.dumps({"assistant_messages_completed": 1}), encoding="utf-8"
    )
    database = tmp_path / "runtime.sqlite3"
    _create_report_database(database)

    report = write_report(results, database)

    assert report["http"]["requests"] == 100
    assert report["http"]["sqlite_locked"] == 1
    assert report["http"]["unhandled_exceptions"] == 1
    assert report["database"]["cache_hit_rate"] == 1
    assert report["providers"]["rail"]["calls"] == 4
    assert report["stages"][1]["requests"] == 200
    assert report["machine"]["cpu"] == "test-cpu"
    assert (results / "summary.md").is_file()


def _create_report_database(path: Path) -> None:
    session = {
        "steps": [
            {
                "name": "rail_outbound",
                "status": "completed",
                "cache_hit": True,
                "duration_ms": 20,
            }
        ]
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE planning_sessions (status TEXT, session_json TEXT)"
        )
        connection.execute(
            "CREATE TABLE trips (itinerary_json TEXT)"
        )
        connection.execute("CREATE TABLE travel_dialogue_sessions (session_id TEXT)")
        connection.execute("CREATE TABLE travel_dialogue_requests (message_id TEXT)")
        connection.execute("CREATE TABLE provider_cache (cache_key TEXT)")
        connection.execute(
            "INSERT INTO planning_sessions VALUES (?, ?)",
            ("awaiting_selection", json.dumps(session)),
        )
