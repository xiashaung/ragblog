"""Shared FastAPI dependencies: RBAC auth for the admin console."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import services
from .db import get_db
from .rbac import PERMISSION_CATALOG
from .services.rbac_service import Principal


def extract_api_key(request: Request) -> str | None:
    """Pull a caller-supplied secret from ``X-API-Key`` (or bearer fallback).

    仅用于首次引导：``/admin/onboard`` 从 ``X-API-Key`` 读取 ``APP_ADMIN_API_KEY``
    证明调用方是运维本人。平台不再有「调用 Key」体系。
    """
    key = request.headers.get("X-API-Key")
    if key:
        return key
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def _extract_bearer(request: Request) -> str:
    """Console token lives in the Authorization header, with a cookie fallback."""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    return token or request.cookies.get("admin_token", "")


async def require_admin_principal(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> Principal:
    """Console auth gate: valid bearer token **and** an active account.

    Returns the :class:`Principal` (roles + merged permissions) so that
    ``require_permission`` never has to re-query.
    """
    token = _extract_bearer(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Admin login required")
    row = await services.admin_service.verify_token(session, token)
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Admin token expired or invalid")
    principal = await services.rbac_service.get_principal(session, row.admin_id)
    if principal is None:
        # 会话还活着但账号已被删除 / 停用：让前端走重新登录流程
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "账号已停用或不存在，请重新登录"
        )
    return principal


async def require_admin_session(
    principal: Principal = Depends(require_admin_principal),
) -> str:
    """Login-only gate for endpoints that act on the caller itself (``/admin/me``)."""
    return principal.admin_id


def require_permission(*codes: str, require_all: bool = True):
    """Route guard factory for RBAC-protected console endpoints.

    Args:
        codes: permission codes required, e.g. ``"key:manage"``.
        require_all: ``True`` needs every code; ``False`` needs any one.

    The returned coroutine resolves the principal and raises 403 (with the
    missing permission labels) when the check fails.
    """
    wanted = tuple(dict.fromkeys(codes))  # 去重但保留顺序

    async def dependency(
        principal: Principal = Depends(require_admin_principal),
    ) -> Principal:
        missing = [code for code in wanted if not principal.can(code)]
        if missing and (require_all or len(missing) == len(wanted)):
            labels = "、".join(_permission_label(code) for code in missing)
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"权限不足，需要：{labels}",
            )
        return principal

    return dependency


def _permission_label(code: str) -> str:
    """403 文案里用中文名而不是裸 code，避免用户看到看不懂的字串。"""
    for item in PERMISSION_CATALOG:
        if item["code"] == code:
            return f"{item['label']}（{code}）"
    return code


__all__ = [
    "extract_api_key",
    "require_admin_principal",
    "require_admin_session",
    "require_permission",
]
