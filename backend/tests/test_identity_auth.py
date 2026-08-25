"""匿名身份与 LangGraph 所有权边界测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from langgraph_sdk import Auth

from api.auth import filter_run_create, own_created_thread, scope_store
from api.identity import IdentityCodec, IdentityError, authenticate_identity
from runtime.config import ConfigurationError, Settings


def test_cookie_tampering_and_expiry_are_rejected() -> None:
    """签名被改动或已经过期时不能静默创建新身份。"""

    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    codec = IdentityCodec("s" * 32, 60)
    token, _ = codec.issue("alice", now=now)

    with pytest.raises(IdentityError, match="签名无效") as tampered:
        codec.verify(f"{token}x", now=now)
    assert tampered.value.code == "auth_cookie_tampered"

    with pytest.raises(IdentityError, match="已过期") as expired:
        codec.verify(token, now=now + timedelta(seconds=61))
    assert expired.value.code == "auth_cookie_expired"


def test_signed_auth_only_bootstraps_on_anonymous_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 Cookie 请求只能进入身份签发接口，不能读取业务资源。"""

    settings = _settings(monkeypatch, auth_mode="signed")
    identity = authenticate_identity(
        settings,
        path="/api/auth/anonymous",
        headers={},
        scope={"client": ("203.0.113.8", 1234)},
    )
    assert identity.user_id == "anonymous-bootstrap"

    with pytest.raises(IdentityError) as missing:
        authenticate_identity(
            settings,
            path="/api/trips",
            headers={},
            scope={"client": ("203.0.113.8", 1234)},
        )
    assert missing.value.code == "auth_cookie_missing"


def test_dev_auth_rejects_non_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定开发身份不能被局域网或公网请求复用。"""

    settings = _settings(monkeypatch, auth_mode="dev", environment="development")
    with pytest.raises(IdentityError) as denied:
        authenticate_identity(
            settings,
            path="/threads",
            headers={},
            scope={"client": ("203.0.113.8", 1234)},
        )
    assert denied.value.code == "dev_auth_non_loopback"


@pytest.mark.asyncio
async def test_thread_run_and_store_are_scoped_to_owner() -> None:
    """客户端伪造的 owner 和 Store 用户前缀都会被服务端覆盖。"""

    user = SimpleNamespace(identity="alice")
    thread_context = Auth.types.AuthContext(
        permissions=["travel"], user=user, resource="threads", action="create"
    )
    thread_value = {"metadata": {"owner": "mallory"}}
    await own_created_thread(thread_context, thread_value)
    assert thread_value["metadata"]["owner"] == "alice"

    run_context = Auth.types.AuthContext(
        permissions=["travel"], user=user, resource="threads", action="create_run"
    )
    run_value = {"metadata": {"owner": "mallory"}}
    assert await filter_run_create(run_context, run_value) == {"owner": "alice"}
    assert run_value["metadata"]["owner"] == "alice"

    store_context = Auth.types.AuthContext(
        permissions=["travel"], user=user, resource="store", action="get"
    )
    store_value = {"namespace": ("trips",), "key": "trip-1"}
    assert await scope_store(store_context, store_value) is True
    assert store_value["namespace"] == ("alice", "trips")


def test_unsafe_production_is_rejected_but_planning_can_load_without_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产开发身份被拒绝；独立 TravelGraph 配置不再强制依赖 Catalog。"""

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("PROVIDER_MODE", "fake")
    with pytest.raises(ConfigurationError, match="生产环境"):
        Settings.from_env()

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AUTH_MODE", "signed")
    monkeypatch.setenv("AUTH_SECRET", "s" * 32)
    monkeypatch.setenv("PROVIDER_MODE", "live")
    monkeypatch.delenv("CATALOG_DATABASE_URL", raising=False)
    settings = Settings.from_env()
    assert settings.catalog_database_url is None


def _settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth_mode: str,
    environment: str = "test",
) -> Settings:
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("AUTH_MODE", auth_mode)
    monkeypatch.setenv("AUTH_SECRET", "s" * 32)
    monkeypatch.setenv("PROVIDER_MODE", "fake")
    return Settings.from_env()
