"""自定义行程历史 HTTP 接口测试。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

from api.identity import IdentityCodec
from api.web import _get_store, create_app
from domain.models import (
    BudgetBreakdown,
    City,
    DayDraft,
    ItineraryDraft,
    TravelRequirements,
    TravelSelection,
    TripRecord,
)
from runtime.config import Settings, get_settings


def test_custom_app_exposes_business_routes() -> None:
    """自定义应用不再附带 docs/openapi 路由，平台能力统一由 Agent Server 提供。"""

    routes = {
        (method, route.path)
        for route in create_app().routes
        for method in getattr(route, "methods", set())
    }
    assert routes == {
        ("POST", "/api/auth/anonymous"),
        ("GET", "/api/trips"),
        ("GET", "/api/trips/{trip_id}"),
        ("DELETE", "/api/trips/{trip_id}"),
    }


def test_anonymous_cookie_is_httponly_lax_and_tampering_is_rejected(
    monkeypatch,
) -> None:
    """身份接口不在 JSON 暴露 Token，并设置要求的浏览器 Cookie 属性。"""

    settings = _signed_settings(monkeypatch)
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(application)

    response = client.post("/api/auth/anonymous")
    assert response.status_code == 200
    assert set(response.json()) == {"user_id", "expires_at"}
    cookie_header = response.headers["set-cookie"].lower()
    assert "httponly" in cookie_header
    assert "samesite=lax" in cookie_header

    current = client.cookies.get(settings.cookie_name)
    client.cookies.set(settings.cookie_name, f"{current}x")
    denied = client.post("/api/auth/anonymous")
    assert denied.status_code == 401
    assert denied.json()["detail"] == "auth_cookie_tampered"


def test_trip_history_isolated_by_store_namespace(monkeypatch) -> None:
    """两个匿名用户即使知道相同行程 ID，也不能跨命名空间读取或删除。"""

    settings = _signed_settings(monkeypatch)
    store = InMemoryStore()
    alice_trip = _trip("alice", "杭州")
    bob_trip = _trip("bob", "成都")
    store.put(("alice", "trips"), str(alice_trip.trip_id), alice_trip.model_dump(mode="json"))
    store.put(("bob", "trips"), str(bob_trip.trip_id), bob_trip.model_dump(mode="json"))

    application = create_app()
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[_get_store] = lambda: store
    client = TestClient(application)
    alice_token, _ = IdentityCodec(settings.signing_secret, settings.cookie_ttl_seconds).issue(
        "alice"
    )
    client.cookies.set(settings.cookie_name, alice_token)

    history = client.get("/api/trips")
    assert history.status_code == 200
    assert [item["destination"] for item in history.json()] == ["杭州"]
    assert client.get(f"/api/trips/{bob_trip.trip_id}").status_code == 404
    assert client.delete(f"/api/trips/{bob_trip.trip_id}").status_code == 404

    deleted = client.delete(f"/api/trips/{alice_trip.trip_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/trips/{alice_trip.trip_id}").status_code == 404


def _signed_settings(monkeypatch) -> Settings:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AUTH_MODE", "signed")
    monkeypatch.setenv("AUTH_SECRET", "s" * 32)
    monkeypatch.setenv("PROVIDER_MODE", "fake")
    return Settings.from_env()


def _trip(user_id: str, city: str) -> TripRecord:
    trip_id = uuid4()
    return TripRecord(
        trip_id=trip_id,
        user_id=user_id,
        requirements=TravelRequirements(
            origin="上海",
            destination=city,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 1),
        ),
        city=City(name=city),
        selection=TravelSelection(
            self_arranged_outbound=True,
            self_arranged_return=True,
            self_arranged_hotel=True,
        ),
        draft=ItineraryDraft(
            summary=f"{city}一日行程",
            days=[DayDraft(day_index=1, theme="城市漫步")],
        ),
        weather=[],
        routes={},
        budget=BudgetBreakdown(),
        created_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
    )
