"""Assistant 会话与旅行工单的无状态签名令牌。

令牌是浏览器和后端之间的短期凭证，不是数据库，也不是可由前端自由修改的状态容器。
载荷包含版本、类型、用户、签发时间、过期时间和 Pydantic 数据；任何字段被修改都会
导致 HMAC 校验失败。Assistant Session Token 只恢复对话快照，TravelOrderToken 只允许
TravelGraph 恢复已经确认的工单，二者通过 ``kind`` 严格隔离。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

PayloadT = TypeVar("PayloadT", bound=BaseModel)


class TokenError(RuntimeError):
    """签名令牌无效、过期或不属于当前身份。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SignedPayloadCodec:
    """使用 HMAC-SHA256 签发带类型、所有者和过期时间的 JSON 载荷。

    验证顺序遵循“先认证、后解析业务”：先比对签名，再检查令牌类型、所有者和时间窗口，
    最后把数据恢复为调用方指定的 Pydantic 类型。调用方不得只解码 Base64 而跳过
    ``verify``。
    """

    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("令牌签名密钥至少需要 32 个字符")
        self._secret = hmac.new(
            secret.encode("utf-8"), b"openzltravel-signed-payload-v1", hashlib.sha256
        ).digest()

    def issue(
        self,
        kind: str,
        owner: str,
        payload: BaseModel,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> str:
        """签发版本化令牌。

        ``kind`` 区分会话令牌和工单令牌，``owner`` 防止跨用户重放，``ttl_seconds``
        控制暴露窗口。业务载荷必须在签发前已经通过 Pydantic 校验。
        """

        if ttl_seconds <= 0:
            raise ValueError("令牌有效期必须大于零")
        issued_at = _utc(now)
        body = {
            "v": 1,
            "kind": kind,
            "sub": owner,
            "iat": int(issued_at.timestamp()),
            "exp": int((issued_at + timedelta(seconds=ttl_seconds)).timestamp()),
            "data": payload.model_dump(mode="json"),
        }
        encoded = _encode(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
        signature = _encode(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify(
        self,
        token: str,
        kind: str,
        owner: str,
        payload_type: type[PayloadT],
        *,
        now: datetime | None = None,
    ) -> PayloadT:
        """验证令牌边界并恢复强类型业务载荷。

        方法只返回通过签名、身份、有效期和模型校验的数据；任何失败都转换为带稳定
        ``code`` 的 ``TokenError``，由 HTTP 或 Graph 边界决定如何向用户展示。
        """

        encoded, signature = _split(token)
        expected = _encode(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise TokenError("token_tampered", "签名令牌已被篡改")
        body = _decode(encoded)
        if body.get("v") != 1 or body.get("kind") != kind:
            raise TokenError("token_kind_invalid", "签名令牌类型无效")
        if body.get("sub") != owner:
            raise TokenError("token_owner_mismatch", "签名令牌不属于当前用户")
        issued_at = body.get("iat")
        expires_at = body.get("exp")
        if (
            not isinstance(issued_at, int)
            or not isinstance(expires_at, int)
            or issued_at > expires_at
        ):
            raise TokenError("token_time_invalid", "签名令牌时间字段无效")
        if expires_at <= int(_utc(now).timestamp()):
            raise TokenError("token_expired", "签名令牌已过期")
        try:
            return payload_type.model_validate(body.get("data"))
        except ValidationError as error:
            raise TokenError("token_payload_invalid", "签名令牌载荷无效") from error


def _split(token: str) -> tuple[str, str]:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError as error:
        raise TokenError("token_invalid", "签名令牌格式无效") from error
    if not encoded or not signature:
        raise TokenError("token_invalid", "签名令牌格式无效")
    return encoded, signature


def _decode(encoded: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TokenError("token_invalid", "签名令牌格式无效") from error
    if not isinstance(value, dict):
        raise TokenError("token_invalid", "签名令牌载荷无效")
    return value


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
