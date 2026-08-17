"""匿名访客身份与旧数据认领。

Cookie 只保存高熵随机 Token，数据库只保存 SHA-256 哈希。业务资源的所有权校验仍由
Repository 在 SQL 中同时匹配 ``visitorid`` 和资源 ID，不能只依赖前端隐藏链接。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

from fastapi import Request, Response

from app.errors import VisitorClaimError
from app.storage import RepositoryConflictError

COOKIE_NAME = "openzltravelvisitor"
COOKIE_MAX_AGE_SECONDS = 365 * 24 * 60 * 60
TOKEN_BYTES = 32


class IdentityRepository(Protocol):
    """匿名身份所需的最小持久化边界。"""

    def get_or_create_visitor(self, token_hash: str, expires_at: datetime) -> UUID:
        """按 Token 哈希读取或创建访客。"""

    def claim_legacy(self, visitor_id: UUID, token_hash: str) -> None:
        """将旧数据原子转移给当前访客。"""


class AnonymousIdentityService:
    """解析 HttpOnly Cookie，并返回当前浏览器的匿名访客编号。"""

    def __init__(self, repository: IdentityRepository, secure_cookie: bool = False) -> None:
        self._repository = repository
        self._secure_cookie = secure_cookie

    def resolve(self, request: Request, response: Response) -> UUID:
        """读取或创建匿名身份，并在需要时写回安全 Cookie。"""

        raw_token = request.cookies.get(COOKIE_NAME)
        if _valid_token(raw_token):
            token = str(raw_token)
        else:
            token = secrets.token_urlsafe(TOKEN_BYTES)
            self._set_cookie(response, token)
        expires_at = _now() + timedelta(seconds=COOKIE_MAX_AGE_SECONDS)
        return self._repository.get_or_create_visitor(_hash_token(token), expires_at)

    def claim(self, visitor_id: UUID, token: str) -> None:
        """认领一次性旧数据；错误码不暴露旧资源细节。"""

        try:
            self._repository.claim_legacy(visitor_id, _hash_token(token))
        except RepositoryConflictError as error:
            code = str(error)
            messages = {
                "visitor_claim_invalid": "认领码无效",
                "visitor_claim_expired": "认领码已过期",
                "visitor_claim_used": "认领码已经使用",
            }
            raise VisitorClaimError(code, messages.get(code, "旧数据认领失败")) from error

    def _set_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            key=COOKIE_NAME,
            value=token,
            max_age=COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            secure=self._secure_cookie,
            samesite="lax",
            path="/",
        )


def _valid_token(token: str | None) -> bool:
    return bool(token and 32 <= len(token) <= 128)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)
