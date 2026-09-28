"""控制台后台路由（RBAC 保护）。

鉴权：一个由 ``services.admin_service`` 签发、以摘要形式存在 ``admin_sessions``
里的 Bearer token；授权：每个管理接口都用
``deps.require_permission(...)`` 声明自己需要的权限码（码表在 ``app/rbac.py``）。

公开的只有两个：``/admin/onboard``（只能建首个账号）与 ``/admin/login``。
SPA 把 token 放在 ``Authorization: Bearer ...`` 里，同时也写一份 ``admin_token``
cookie，好让刷新逻辑在请求没带头时能退回读 cookie。
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
from ..rate_limit import client_key, login_limiter
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

# 管理 API 统一收进 /api/v1/admin：/admin 路径留给控制台 SPA 页面（见 main.py 静态托管），
# 否则刷新 /admin/roles 这类页面会先命中 API 路由、浏览器里看到一屏 JSON。
router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _extract_token(request) -> str:
    """尽量拿到 token：先看 Authorization 头，再退到 ``admin_token`` cookie。"""
    bearer = request.headers.get("Authorization", "")
    if bearer.startswith("Bearer "):
        return bearer[7:].strip()
    return request.cookies.get("admin_token", "")


def _enforce_login_limit(key: str) -> None:
    """登录/引导入口的统一闸：被限就 429，顺带报还要等多久。"""
    settings = get_settings()
    if not settings.login_rate_limit_enabled:
        return
    wait = login_limiter.retry_after(key, settings=settings)
    if wait > 0:
        seconds = int(wait) + 1
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"尝试过于频繁，请 {seconds} 秒后再试",
            headers={"Retry-After": str(seconds)},
        )


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
    """建第一个超管账号（公网上唯一的首次入口）。

    库里已有账号就失败，所以反复调也不会提权；只给首次引导 / ``seed`` CLI 用。
    之后新建账号请走 ``POST /admin/accounts``（需 ``account:manage``）。

    先用引导密钥证明「是运维本人」，再才去建账号；服务层的
    ``ValueError``（已存在账号）翻译成 409。密钥猜测也吃限流：
    引导窗内错满次数同样锁定，公网暴露的首次入口不能裸奔。
    """
    key = client_key(request, "onboard")
    _enforce_login_limit(key)
    login_limiter.record_attempt(key)
    try:
        _ensure_bootstrap_key(request)
    except HTTPException:
        login_limiter.record_failure(key)
        raise
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
    """账号口令登录：返 token + 角色/权限，同时下发 httpOnly cookie。

    口令错 / 账号停用都归 401（服务层的 ``ValueError`` 在这里接）。
    限流两道闸（见 app/rate_limit.py）：尝试窗防扫接口，失败锁定防爆口令；
    账号不存在与口令错都计入失败，不给探测「哪个用户名存在」留差异。
    """
    key = client_key(request, payload.username)
    _enforce_login_limit(key)
    login_limiter.record_attempt(key)
    try:
        result = await services.admin_service.login(session, payload.username, payload.password)
    except ValueError as exc:
        login_limiter.record_failure(key)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, str(exc)
        ) from exc
    login_limiter.record_success(key)
    expires_in = int((result.expires_at - time.time()) or 0)
    response.set_cookie(
        "admin_token",
        result.token,
        httponly=True,
        samesite="lax",
        max_age=expires_in,
        secure=False,  # 上 HTTPS 后要改成 True
    )
    return result


@router.post("/logout")
async def logout(
    response: Response,
    request: Request,
    session: AsyncSession = Depends(get_db),
    admin_id: str = Depends(deps.require_admin_session),
) -> dict:
    """登出：删掉本次 token 对应的会话行，并抹掉 cookie。"""
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
    """旋转调用方自己的 token：旧的当场作废，新的返回去。"""
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
    """列出当前未过期的会话（看哪个账号在几台设备上登录着）。"""
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
    """角色列表（带成员数）。"""
    return await services.rbac_service.list_roles(session)


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreate,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.ROLE_MANAGE)),
) -> dict:
    """新建角色：把当前身份作为 ``actor`` 传下去，过提权与通配码两道闸门。"""
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
    """改角色名 / 描述 / 权限集；服务层的 ``ValueError`` 由全局处理器转 400。"""
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
    """删角色（内置角色 / 还有成员时拒删）。"""
    await services.rbac_service.delete_role(session, role_id)
    return {"deleted": True}


# --- 账号 ---


async def _account_payload(session: AsyncSession, admin: AdminUser) -> AccountOut:
    """拼账号响应：额外查一次它当前生效的角色 code。"""
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
    """账号列表 + 各自角色。"""
    return await services.rbac_service.list_accounts(session)


@router.post("/accounts", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: AccountCreate,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.ACCOUNT_MANAGE)),
) -> AccountOut:
    """新建后台账号并分配角色（分配超管类角色只有超管能做）。"""
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
    """改名 / 启停 / 重置密码；停用与改密都会踢掉该账号的在线会话。"""
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
    """覆盖式设置账号角色（收回最后一个超管会被服务层拒）。"""
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
    """删账号（不能自删、不能删最后一个可用超管）；``actor`` 用于这两项判定。"""
    await services.rbac_service.delete_account(session, account_id, actor=principal)
    return {"deleted": True}
