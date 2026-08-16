import asyncio
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import ApplicationContainer, app, get_planning_runtime, get_travel_service
from app.storage import SqliteTripRepository
from app.travel import TravelService
from tests.fakes import FakeMapProvider, FakePlanner
from tests.test_workbench import make_runtime, planning_request


def client_for(tmp_path: Path) -> TestClient:
    service = TravelService(
        map_provider=FakeMapProvider(),
        planner=FakePlanner(),
        repository=SqliteTripRepository(str(tmp_path / "api.sqlite3")),
    )
    app.dependency_overrides[get_travel_service] = lambda: service
    return TestClient(app)


def test_health() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_local_frontend_cors_allows_dynamic_vite_port() -> None:
    """Vite 切换端口后仍应能够访问本机 API。"""

    response = TestClient(app).options(
        "/api/planning-sessions",
        headers={
            "Origin": "http://127.0.0.1:5180",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,idempotency-key",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5180"


@pytest.mark.asyncio
async def test_application_container_closes_runtime_and_all_http_clients(tmp_path: Path) -> None:
    """容器关闭必须回收后台任务、同步地图客户端和异步 MCP 连接池。"""

    container = ApplicationContainer(
        Settings(
            database_path=str(tmp_path / "container.sqlite3"),
            catalog_path=str(tmp_path / "missing-catalog.sqlite3"),
            rollinggo_hotel_token_path=str(tmp_path / "missing-token.json"),
            llm_api_key="",
            llm_model="",
        )
    )

    await container.close()
    await container.close()

    assert container.runtime.tasks == {}
    assert container.amap_client.http.is_closed
    assert container.weather_client.http.is_closed
    assert all(client.http.is_closed for client in container.provider_clients)


def test_trip_api_lifecycle(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    response = client.post(
        "/api/trips",
        json={
            "destination": "测试市",
            "start_date": date(2026, 9, 1).isoformat(),
            "end_date": date(2026, 9, 2).isoformat(),
            "travelers": 2,
            "budget": 3000,
        },
    )
    assert response.status_code == 201
    trip_id = response.json()["trip_id"]
    assert response.json()["days"][0]["budget"]["total"] > 0
    assert response.json()["days"][0]["activities"][0]["image_url"].endswith("a1.jpg")

    assert client.get("/api/trips").json()[0]["trip_id"] == trip_id
    assert client.get(f"/api/trips/{trip_id}").status_code == 200
    export = client.get(f"/api/trips/{trip_id}/export/markdown")
    assert export.status_code == 200
    assert "当日预算" in export.text
    assert client.delete(f"/api/trips/{trip_id}").status_code == 204
    assert client.get(f"/api/trips/{trip_id}").status_code == 404


def test_api_validation_error_is_stable(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.post(
        "/api/trips",
        json={
            "destination": "测试市",
            "start_date": "2026-09-08",
            "end_date": "2026-09-01",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_planning_session_api_lifecycle(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    app.dependency_overrides[get_planning_runtime] = lambda: runtime
    app.dependency_overrides[get_travel_service] = lambda: runtime.travel_service
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            request = planning_request().model_dump(mode="json")
            created = await client.post(
                "/api/planning-sessions",
                json=request,
                headers={"Idempotency-Key": "api-lifecycle"},
            )
            duplicate = await client.post(
                "/api/planning-sessions",
                json=request,
                headers={"Idempotency-Key": "api-lifecycle"},
            )
            assert created.status_code == duplicate.status_code == 202
            session_id = created.json()["session_id"]
            assert duplicate.json()["session_id"] == session_id

            discovered = await _poll_session(client, session_id, "awaiting_selection")
            selected = await client.put(
                f"/api/planning-sessions/{session_id}/selection",
                json={
                    "outbound": {"option_id": discovered["outbound_options"][0]["option_id"]},
                    "return_trip": {"option_id": discovered["return_options"][0]["option_id"]},
                    "hotel_id": discovered["hotel_options"][0]["hotel_id"],
                },
            )
            assert selected.status_code == 200

            hotel = await client.get(
                f"/api/planning-sessions/{session_id}/hotels/"
                f"{discovered['hotel_options'][0]['hotel_id']}"
            )
            assert hotel.status_code == 200
            assert hotel.json()["name"] == discovered["hotel_options"][0]["name"]

            generated = await client.post(f"/api/planning-sessions/{session_id}/generate")
            assert generated.status_code == 202
            completed = await _poll_session(client, session_id, "completed")
            trip_id = completed["trip_id"]

            trip = await client.get(f"/api/trips/{trip_id}")
            alternatives = await client.get(f"/api/trips/{trip_id}/alternatives")
            assert trip.status_code == alternatives.status_code == 200
            activity = trip.json()["days"][0]["activities"][0]
            edited = await client.patch(
                f"/api/trips/{trip_id}/days/1",
                json={
                    "expected_revision": 1,
                    "activities": [
                        {
                            "poi_id": activity["poi_id"],
                            "start_time": "10:00",
                            "duration_minutes": 90,
                        }
                    ],
                },
            )
            assert edited.status_code == 200
            assert edited.json()["revision"] == 2
    finally:
        app.dependency_overrides.pop(get_planning_runtime, None)
        app.dependency_overrides.pop(get_travel_service, None)


async def _poll_session(
    client: httpx.AsyncClient, session_id: str, expected: str
) -> dict[str, object]:
    for _ in range(100):
        response = await client.get(f"/api/planning-sessions/{session_id}")
        payload = response.json()
        if payload["status"] == expected:
            return payload
        assert payload["status"] != "failed", payload.get("error_message")
        await asyncio.sleep(0.01)
    raise AssertionError(f"规划会话未进入 {expected}")
