"""Admin authentication repository, backed by the shared session.

All methods take a ``db.AsyncSession`` — the session is owned by the
router via ``get_db``. Authn is via a randomly generated bearer token:
- ``login`` looks up the admin by username, verifies the password, then
  blacklists *existing* live sessions for that account and mints a new one.
- ``refresh`` accepts a valid token, validates it, then blacklists the
  used one and mints a replacement.
- ``logout`` (and token *expiry*) simply delete the token's row, turning
  a token into a no-op. Storing only the SHA-256 digest of the token
  means an extracted ``Authorization`` header value is hashed on read
  before the lookup.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import AdminSession, AdminUser
from ..schemas.admin import AdminLoginResponse, AdminToken


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _now() -> float:
    return time.time()


def _ttl() -> float:
    """Seconds a freshly issued token remains valid."""
    settings = get_settings()
    return getattr(settings, "admin_session_ttl_seconds", 86_400)


def _generate_token(admin_id: str) -> str:
    return f"admin_{uuid.uuid4().hex[:24]}-{secrets.token_urlsafe(16)}"


def _login_response(
    plain_token: str,
    expires_at: float,
    admin: AdminUser,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> AdminLoginResponse:
    token = AdminToken(
        token=plain_token,
        token_type="bearer",
        expires_at=expires_at,
    )
    return AdminLoginResponse(
        token=token.token,
        token_type=token.token_type,
        expires_at=token.expires_at,
        admin={
            "id": admin.id,
            "username": admin.username,
            "name": admin.name,
            "is_active": admin.is_active,
        },
        # 前端登录后直接拿它渲染菜单 / 按钮，不必再请求 /admin/me
        roles=roles or [],
        permissions=permissions or [],
    )


async def login(session: AsyncSession, username: str, password: str) -> AdminLoginResponse:
    from . import rbac_service

    row = (
        await session.scalar(select(AdminUser).where(AdminUser.username == username))
    )
    if row is None or row.password_hash != hash_password(password):
        raise ValueError("Invalid username or password")
    if not row.is_active:
        raise ValueError("This account has been disabled")

    window = _now() - _ttl()
    # Blacklist live sessions for this account so that every login
    # forces previously-issued tokens (e.g. on other devices) to expire.
    existing = (
        await session.scalars(
            select(AdminSession).where(
                AdminSession.admin_id == row.id,
                AdminSession.expires_at >= window,
            )
        )
    ).all()
    for live in existing:
        await session.delete(live)

    new_plain = _generate_token(row.id)
    new_row = AdminSession(
        token_hash=_hash_token(new_plain),
        admin_id=row.id,
        expires_at=_now() + _ttl(),
    )
    session.add(new_row)
    await session.flush()
    roles, permissions = await rbac_service.permissions_for(session, row.id)
    return _login_response(new_plain, new_row.expires_at, row, roles, permissions)


async def verify_token(session: AsyncSession, token: str) -> AdminSession | None:
    """Validate a bare token string (no rotation). Returns None if absent/unknown."""
    if not token:
        return None
    row = (
        await session.execute(
            select(AdminSession).where(AdminSession.token_hash == _hash_token(token))
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if _now() > row.expires_at:
        await session.delete(row)
        await session.flush()
        return None
    return row


async def refresh_token(session: AsyncSession, token: str) -> AdminLoginResponse:
    """Swap a valid token for a new one, invalidating the old."""
    from . import rbac_service

    row = await verify_token(session, token)
    if row is None:
        raise ValueError("Token expired or invalid")
    await session.delete(row)
    new_plain = _generate_token(row.admin_id)
    new_row = AdminSession(
        token_hash=_hash_token(new_plain),
        admin_id=row.admin_id,
        expires_at=_now() + _ttl(),
    )
    session.add(new_row)
    await session.flush()
    admin = await session.scalar(select(AdminUser).where(AdminUser.id == new_row.admin_id))
    roles, permissions = await rbac_service.permissions_for(session, new_row.admin_id)
    return _login_response(new_plain, new_row.expires_at, admin, roles, permissions)


async def logout(session: AsyncSession, token: str) -> bool:
    row = (await session.execute(
        select(AdminSession).where(AdminSession.token_hash == _hash_token(token))
    )).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def list_live_sessions(session: AsyncSession) -> list[dict]:
    """Return live sessions (id, username, expiry) for the dashboard."""
    rows = (await session.scalars(
        select(AdminSession).order_by(AdminSession.created_at)
    )).all()
    out = []
    for row in rows:
        admin = await session.scalar(
            select(AdminUser).where(AdminUser.id == row.admin_id)
        )
        out.append(
            {
                "id": row.id,
                "username": admin.username if admin else "",
                "admin_id": row.admin_id,
                "expires_at": row.expires_at,
            }
        )
    return out


async def revoke_session(session: AsyncSession, session_id: str) -> bool:
    """Force one live session offline (returns False when the id is unknown)."""
    row = await session.get(AdminSession, session_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


__all__ = [
    "login",
    "create_admin",
    "hash_password",
    "verify_token",
    "refresh_token",
    "logout",
    "list_live_sessions",
    "revoke_session",
]


async def create_admin(
    session: AsyncSession, username: str, password: str, name: str = ""
) -> AdminUser:
    """Create the first super-admin account (guarded: fails if one exists).

    Used by the public onboarding route and the ``seed`` CLI. RBAC 后「创建账号」
    不再是公开能力（走 ``/admin/accounts`` + ``account:manage``），这里只保留
    「首次引导」：库内已有账号就拒绝，并把新账号绑到内置 superadmin 角色。
    """
    from . import rbac_service

    existing = (await session.scalars(select(AdminUser))).first()
    if existing is not None:
        raise ValueError("Admin accounts already exist; use login instead")
    await rbac_service.ensure_builtin_roles(session)
    admin = AdminUser(
        id=f"adm_{uuid.uuid4().hex[:12]}",
        username=username,
        password_hash=hash_password(password),
        name=name or username,
        is_active=True,
    )
    session.add(admin)
    await session.flush()
    superadmin = await rbac_service.ensure_superadmin_membership(session, admin.id)
    if superadmin is None:  # pragma: no cover - ensure_builtin_roles 刚创建过
        raise ValueError("内置超级管理员角色缺失，请先执行 alembic upgrade head")
    return admin

