"""匿名身份 Cookie 的签发、验证和请求解析。

Cookie 内只保存随机用户标识和签发/过期时间，完整载荷使用 HMAC-SHA256 签名。
服务端不建立额外身份表；所有权由认证上下文和 LangGraph 元数据/Store 命名空间共同约束。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from ipaddress import ip_address
from typing import Any, Mapping
from uuid import uuid4

from runtime.config import Settings


class IdentityError(RuntimeError):
    """身份 Cookie 不存在、被篡改、过期或请求来源不安全。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Identity:
    """认证完成后的最小匿名身份。"""

    user_id: str
    expires_at: datetime


class IdentityCodec:
    """使用 HMAC-SHA256 签发和验证无状态匿名身份。"""

    def __init__(self, secret: str, ttl_seconds: int) -> None:
        if len(secret) < 32:
            raise ValueError("身份签名密钥至少需要 32 个字符")
        if ttl_seconds <= 0:
            raise ValueError("身份有效期必须大于零")
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds

    def issue(
        self,
        user_id: str | None = None,
        *,
        now: datetime | None = None,
    ) -> tuple[str, Identity]:
        """为指定或新建用户签发 Cookie 值，并返回可展示的身份信息。"""

        issued_at = _utc(now)
        expires_at = issued_at + timedelta(seconds=self._ttl_seconds)
        identity = Identity(user_id or str(uuid4()), expires_at)
        payload = {
            "exp": int(expires_at.timestamp()),
            "iat": int(issued_at.timestamp()),
            "sub": identity.user_id,
        }
        encoded = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = _encode(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
        return f"{encoded}.{signature}", identity

    def verify(self, token: str, *, now: datetime | None = None) -> Identity:
        """验证签名、字段和过期时间；任何异常都转换为稳定身份错误。"""

        encoded, signature = _split_token(token)
        expected = _encode(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise IdentityError("auth_cookie_tampered", "身份 Cookie 签名无效")
        payload = _decode_payload(encoded)
        identity = _identity_from_payload(payload)
        if identity.expires_at <= _utc(now):
            raise IdentityError("auth_cookie_expired", "身份 Cookie 已过期")
        return identity


def cookie_from_headers(headers: Mapping[Any, Any] | None, cookie_name: str) -> str | None:
    """从 ASGI 字节头或普通字符串头中安全提取指定 Cookie。"""

    if not headers:
        return None
    raw = headers.get(b"cookie") or headers.get(b"Cookie")
    if raw is None:
        raw = headers.get("cookie") or headers.get("Cookie")
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1")
    if not isinstance(raw, str):
        return None
    parsed = SimpleCookie()
    try:
        parsed.load(raw)
    except Exception as error:
        raise IdentityError("auth_cookie_invalid", "身份 Cookie 格式无效") from error
    morsel = parsed.get(cookie_name)
    return morsel.value if morsel else None


def authenticate_identity(
    settings: Settings,
    *,
    path: str,
    headers: Mapping[Any, Any] | None,
    scope: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> Identity:
    """按运行模式认证请求；签名模式只对身份签发接口允许无 Cookie 进入。"""

    if settings.auth_mode == "dev":
        _require_loopback(scope, settings.environment)
        expires_at = _utc(now) + timedelta(seconds=settings.cookie_ttl_seconds)
        return Identity(settings.dev_user_id, expires_at)

    token = cookie_from_headers(headers, settings.cookie_name)
    if token:
        return IdentityCodec(settings.signing_secret, settings.cookie_ttl_seconds).verify(
            token, now=now
        )
    if path == "/api/auth/anonymous":
        # 这是唯一可匿名进入的引导路由；它只签发身份，不读取业务资源。
        return Identity("anonymous-bootstrap", _utc(now))
    raise IdentityError("auth_cookie_missing", "缺少身份 Cookie")


def _require_loopback(scope: Mapping[str, Any] | None, environment: str) -> None:
    client = scope.get("client") if scope else None
    host = client[0] if isinstance(client, (tuple, list)) and client else ""
    if environment == "test" and host in {"test", "testclient"}:
        return
    try:
        if ip_address(str(host)).is_loopback:
            return
    except ValueError:
        pass
    raise IdentityError("dev_auth_non_loopback", "开发身份模式只允许本机访问")


def _split_token(token: str) -> tuple[str, str]:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError as error:
        raise IdentityError("auth_cookie_invalid", "身份 Cookie 格式无效") from error
    if not encoded or not signature:
        raise IdentityError("auth_cookie_invalid", "身份 Cookie 格式无效")
    return encoded, signature


def _decode_payload(encoded: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(encoded + padding)
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IdentityError("auth_cookie_invalid", "身份 Cookie 载荷无效") from error
    if not isinstance(payload, dict):
        raise IdentityError("auth_cookie_invalid", "身份 Cookie 载荷无效")
    return payload


def _identity_from_payload(payload: dict[str, Any]) -> Identity:
    user_id = payload.get("sub")
    expires_at = payload.get("exp")
    issued_at = payload.get("iat")
    if not isinstance(user_id, str) or not user_id or len(user_id) > 128:
        raise IdentityError("auth_cookie_invalid", "身份 Cookie 用户字段无效")
    if not isinstance(expires_at, int) or not isinstance(issued_at, int):
        raise IdentityError("auth_cookie_invalid", "身份 Cookie 时间字段无效")
    if issued_at > expires_at:
        raise IdentityError("auth_cookie_invalid", "身份 Cookie 时间范围无效")
    try:
        expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise IdentityError("auth_cookie_invalid", "身份 Cookie 时间字段无效") from error
    return Identity(user_id, expiry)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
