"""共享依赖：管理后台的登录态与 RBAC 权限门。

三个层级，路由按需要选一个（都在 routers/* 里以 ``Depends(...)`` 使用）：

    require_admin_principal  → 拿完整身份（角色 + 合并后的权限码），给要读 Principal 的路由
    require_admin_session    → 只要「登录就行」，给只作用于调用者自己的接口（对话会话等）
    require_permission(...)  → 工厂，校验具体权限码，缺则 403

鉴权口径：token 从 ``Authorization: Bearer`` 读，兼容 ``admin_token`` cookie（两种都能过）；
真正的权限判定在后端，前端存的那份权限只用于渲染菜单。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import services
from .db import get_db
from .rbac import PERMISSION_CATALOG
from .services.rbac_service import Principal


def extract_api_key(request: Request) -> str | None:
    """从 ``X-API-Key``（或 Bearer 头兜底）里取调用方带上的凭据。

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
    """取控制台 token：首选 Authorization 头，没带则回退到 http-only cookie。"""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    return token or request.cookies.get("admin_token", "")


async def require_admin_principal(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> Principal:
    """控制台鉴权门：token 有效 **且** 账号在用。

    直接返回 :class:`Principal`（已带角色与合并后的权限码），这样
    ``require_permission`` 不必为了同一个请求再查一次库。
    三种 401 文案有意区分开：未登录 / token 失效 / 账号已停，便于前端定提示。
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
    """只要求登录的闸门：给「只作用于调用者自己」的接口（不涉权限码）。"""
    return principal.admin_id


def require_permission(*codes: str, require_all: bool = True):
    """RBAC 路由守卫工厂：管理后台的接口都靠它护。

    Args:
        codes: 需要的权限码，如 ``"key:manage"``。
        require_all: ``True`` 要求全部命中；``False`` 任一命中即可。

    返回的依赖先解出 Principal（等于顺带做了登录校验），再比对权限码；
    不足时抛 403，并把缺的权限码翻成中文名字，不让前端看到裸 code。
    超管持有 ``*`` 通配码，``principal.can(...)`` 会直接放行。
    """
    wanted = tuple(dict.fromkeys(codes))  # 去重但保留顺序

    async def dependency(
        principal: Principal = Depends(require_admin_principal),
    ) -> Principal:
        """嵌套依赖：拿 Principal → 比权限码 → 不足则 403。"""
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
