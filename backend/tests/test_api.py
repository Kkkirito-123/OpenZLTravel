from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app, get_travel_service
from app.storage import SqliteTripRepository
from app.travel import TravelService
from tests.fakes import FakeMapProvider, FakePlanner


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

    assert client.get("/api/trips").json()[0]["trip_id"] == trip_id
    assert client.get(f"/api/trips/{trip_id}").status_code == 200
    assert client.get(f"/api/trips/{trip_id}/export/markdown").status_code == 200
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
