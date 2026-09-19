"""Admin dashboard routes (RBAC protected).

Authentication is a valid bearer token minted by ``services.admin_service`` and
stored (hashed) in ``admin_sessions``. Authorization is role based: every
management endpoint declares the permission code it needs via
``deps.require_permission(...)``, codes being defined in ``app/rbac.py``.

Public: ``/admin/onboard`` (first account only), ``/admin/login``.
The SPA carries the token in ``Authorization: Bearer ...`` and also as the
``admin_token`` cookie so refresh logic can fall back to it.
"""

from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from .. import deps, services
from ..config import get_settings
from ..db import get_db
from ..models import AdminUser
from ..rbac import PERMISSION_CATALOG, Permission
from ..schemas.admin import (
    AccountCreate,
    AccountOut,
    AccountRolesUpdate,
    AccountUpdate,
    AdminCreate,
    AdminLoginRequest,
    AdminLoginResponse,
    AdminToken,
    PermissionOut,
    PrincipalOut,
    RoleCreate,
    RoleOut,
    RoleUpdate,
)
from ..services.rbac_service import Principal

router = APIRouter(prefix="/admin", tags=["admin"])


def _extract_token(request) -> str:
    """Best-effort token extraction: Authorization header or admin_token cookie."""
    bearer = request.headers.get("Authorization", "")
    if bearer.startswith("Bearer "):
        return bearer[7:].strip()
    return request.cookies.get("admin_token", "")


def _ensure_bootstrap_key(request: Request) -> None:
    """首次引导密钥校验（代替以前的「admin key 字符串万能旁路」）。

    ``APP_ADMIN_API_KEY`` 现在只剩这一个用途：证明调用方是运维本人，而不是随便
    一个能访问服务的人都能抢注第一个超管。置空 = 关闭 HTTP 引导，只能走 CLI。
    """
    settings = get_settings()
    if not settings.admin_api_key:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "首次引导已关闭：请设置 APP_ADMIN_API_KEY，或用 python -m app.bootstrap seed 创建账号",
        )
    presented = deps.extract_api_key(request) or ""
    if not hmac.compare_digest(presented, settings.admin_api_key):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "引导密钥不正确（需与 APP_ADMIN_API_KEY 一致）"
        )


@router.post("/onboard", response_model=dict, tags=["admin-onboard"])
async def onboard(
    payload: AdminCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Create the very first super-admin account (idempotent-ish guard).

    Fails if any admin already exists, so repeated onboarding calls are safe;
    intended only for first-time setup / ``seed`` CLI. 之后新建账号请走
    ``POST /admin/accounts``（需 ``account:manage``）。
    """
    _ensure_bootstrap_key(request)
    try:
        await services.admin_service.create_admin(
            session, payload.username, payload.password, payload.name
        )
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, str(exc)
        ) from exc
    return {"admin_created": True, "username": payload.username}


@router.post("/login", response_model=AdminLoginResponse)
async def login(
    payload: AdminLoginRequest,
    response: Response,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> AdminLoginResponse:
    try:
        result = await services.admin_service.login(session, payload.username, payload.password)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, str(exc)
        ) from exc
    expires_in = int((result.expires_at - time.time()) or 0)
    response.set_cookie(
        "admin_token",
        result.token,
        httponly=True,
        samesite="lax",
        max_age=expires_in,
        secure=False,  # set True behind HTTPS
    )
    return result


@router.post("/logout")
async def logout(
    response: Response,
    request: Request,
    session: AsyncSession = Depends(get_db),
    admin_id: str = Depends(deps.require_admin_session),
) -> dict:
    token = _extract_token(request)
    await services.admin_service.logout(session, token)
    response.delete_cookie("admin_token")
    return {"logged_out": True, "admin_id": admin_id}


@router.post("/refresh", response_model=AdminToken)
async def refresh(
    request: Request,
    session: AsyncSession = Depends(get_db),
    _admin_id: str = Depends(deps.require_admin_session),
) -> AdminToken:
    """Rotates the caller's token; expires the previously-valid one."""
    token = _extract_token(request)
    try:
        result = await services.admin_service.refresh_token(session, token)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, str(exc)
        ) from exc
    return result


@router.get("/me", response_model=PrincipalOut)
async def me(
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_admin_principal),
) -> PrincipalOut:
    """当前登录身份 + 生效角色 + 合并后的权限集（前端据此渲染菜单/按钮）。

    能走到这里就说明账号是启用的（``get_principal`` 已校验），所以
    ``is_active`` 恒为 True；被停用的账号得 401 重新登录。
    """
    sessions = await services.admin_service.list_live_sessions(session)
    return PrincipalOut(
        id=principal.admin_id,
        username=principal.username,
        name=principal.name,
        is_active=True,
        roles=principal.roles,
        permissions=principal.permissions,
        is_superadmin=principal.is_superadmin,
        live_sessions=len(sessions),
    )


@router.get("/sessions", response_model=list[dict])
async def list_live_sessions(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.SESSION_READ)),
) -> list[dict]:
    return await services.admin_service.list_live_sessions(session)


@router.delete("/sessions/{session_id}")
async def revoke_live_session(
    session_id: str,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.SESSION_REVOKE)),
) -> dict:
    """强制下线某个会话（token 立即失效）。"""
    ok = await services.admin_service.revoke_session(session, session_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在或已失效")
    return {"revoked": True}


# --- 权限目录与角色 ---


@router.get("/permissions", response_model=list[PermissionOut])
async def list_permissions(
    _principal: Principal = Depends(deps.require_permission(Permission.ROLE_READ)),
) -> list[dict]:
    """代码定义的权限码目录（前端渲染角色的勾选框）。"""
    return PERMISSION_CATALOG


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.ROLE_READ)),
) -> list[dict]:
    return await services.rbac_service.list_roles(session)


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreate,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.ROLE_MANAGE)),
) -> dict:
    role = await services.rbac_service.create_role(
        session,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        permissions=payload.permissions,
        actor=principal,
    )
    return await services.rbac_service.role_payload(session, role)


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: str,
    payload: RoleUpdate,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.ROLE_MANAGE)),
) -> dict:
    role = await services.rbac_service.update_role(
        session,
        role_id,
        name=payload.name,
        description=payload.description,
        permissions=payload.permissions,
        actor=principal,
    )
    return await services.rbac_service.role_payload(session, role)


@router.delete("/roles/{role_id}")
async def delete_role(
    role_id: str,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.ROLE_MANAGE)),
) -> dict:
    await services.rbac_service.delete_role(session, role_id)
    return {"deleted": True}


# --- 账号 ---


async def _account_payload(session: AsyncSession, admin: AdminUser) -> AccountOut:
    roles, _permissions = await services.rbac_service.permissions_for(session, admin.id)
    return AccountOut(
        id=admin.id,
        username=admin.username,
        name=admin.name,
        is_active=admin.is_active,
        created_at=admin.created_at,
        roles=roles,
    )


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.ACCOUNT_READ)),
) -> list[dict]:
    return await services.rbac_service.list_accounts(session)


@router.post("/accounts", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: AccountCreate,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.ACCOUNT_MANAGE)),
) -> AccountOut:
    admin = await services.rbac_service.create_account(
        session,
        username=payload.username,
        password=payload.password,
        name=payload.name,
        role_codes=payload.roles,
        actor=principal,
    )
    return await _account_payload(session, admin)


@router.patch("/accounts/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: str,
    payload: AccountUpdate,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.ACCOUNT_MANAGE)),
) -> AccountOut:
    admin = await services.rbac_service.update_account(
        session,
        account_id,
        name=payload.name,
        is_active=payload.is_active,
        password=payload.password,
        actor=principal,
    )
    return await _account_payload(session, admin)


@router.put("/accounts/{account_id}/roles", response_model=AccountOut)
async def set_account_roles(
    account_id: str,
    payload: AccountRolesUpdate,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.ACCOUNT_MANAGE)),
) -> AccountOut:
    admin = await services.rbac_service.set_account_roles(
        session, account_id, payload.roles, actor=principal
    )
    return await _account_payload(session, admin)


@router.delete("/accounts/{account_id}")
async def delete_account(
    account_id: str,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.ACCOUNT_MANAGE)),
) -> dict:
    await services.rbac_service.delete_account(session, account_id, actor=principal)
    return {"deleted": True}
