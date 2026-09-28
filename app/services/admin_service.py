"""控制台账号的认证仓库，跑在共享的数据库会话上。

所有方法都接受一个 ``db.AsyncSession``：会话由路由通过 ``get_db`` 拿到并持有，
本层只 ``flush``、不 ``commit``。认证凭据是一个随机生成的 Bearer token，
库里只存它的 SHA-256 摘要，因此拿到的 ``Authorization`` 头要先哈希再查：

- ``login``：按用户名找账号、验口令（Argon2，兼容旧 SHA-256 并验过即升级写回），
  然后**清掉该账号已有的未过期会话**再签一个新的
  （所以一次登录会把其他设备上的旧 token 一并作废）。
- ``refresh_token``：验一个有效 token，验过就把旧的删掉、签一个替换的（旋转）。
- ``logout``（以及 token 自然过期）只是把这一行删掉，让 token 直接变成无效值。
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import AdminSession, AdminUser
from ..schemas.admin import AdminLoginResponse, AdminToken

# Argon2id 口令哈希器（默认参数 m=64MiB/t=3/p=4）。登录校验是 CPU 密集的，
# 都在 async 路由里同步调用；单管理后台的并发量级可接受，若登录 QPS 上来
# 应改用 Starlette 线程池依赖把校验挪出事件循环。
_hasher = PasswordHasher()

# 旧版口令哈希是裸 SHA-256（64 位十六进制）。留着它只为「平滑迁移」：
# 存量账号仍可用口令登录，验过即升级为 Argon2 写回（见 login）。
_SHA256_LEN = 64


def _hash_token(token: str) -> str:
    """token → 库里存放的摘要；校验时也必须走同一个函数。"""
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    """口令摘要（Argon2id）。与种子脚本、``create_admin`` 共用，保证写入与比对一致。"""
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """校验口令，兼容新旧两种哈希：SHA-256 直比 / Argon2 走验证器。"""
    if len(stored_hash) == _SHA256_LEN and all(c in "0123456789abcdef" for c in stored_hash):
        return secrets.compare_digest(stored_hash, hashlib.sha256(password.encode()).hexdigest())
    try:
        _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False
    return True


def needs_password_upgrade(stored_hash: str) -> bool:
    """存的还是旧 SHA-256，或 Argon2 参数已落后于当前哈希器 → 需要重写。"""
    if len(stored_hash) == _SHA256_LEN and all(c in "0123456789abcdef" for c in stored_hash):
        return True
    return _hasher.check_needs_rehash(stored_hash)


def _now() -> float:
    """统一的「现在」：epoch 秒，与 ``expires_at`` 同一标度。"""
    return time.time()


def _ttl() -> float:
    """新签 token 的有效期（秒）。"""
    settings = get_settings()
    return getattr(settings, "admin_session_ttl_seconds", 86_400)


def _generate_token(admin_id: str) -> str:
    """造一个不可猜的明文 token；``admin_`` 前缀方便日志里一眼认出类型。"""
    return f"admin_{uuid.uuid4().hex[:24]}-{secrets.token_urlsafe(16)}"


def _login_response(
    plain_token: str,
    expires_at: float,
    admin: AdminUser,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> AdminLoginResponse:
    """把「明文 token + 账号 + 权限」拼成登录响应（登录与刷新共用）。"""
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
    """账号口令登录：失败一律 ``ValueError``（路由翻译成 400），成功返 token + 权限。"""
    from . import rbac_service

    row = (
        await session.scalar(select(AdminUser).where(AdminUser.username == username))
    )
    if row is None or not verify_password(row.password_hash, password):
        raise ValueError("Invalid username or password")
    if not row.is_active:
        raise ValueError("This account has been disabled")

    # 旧 SHA-256（或参数过时的 Argon2）验过即就地升级写回，存量数据逐步淘汰
    if needs_password_upgrade(row.password_hash):
        row.password_hash = hash_password(password)

    window = _now() - _ttl()
    # 清掉该账号所有还在有效期内的会话：一次登录就把其他设备上签发过的 token 一并作废
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
    """只校验不旋转：token 缺失 / 不认识 / 已过期都返 ``None``（过期顺手删行）。"""
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
    """用旧 token 换新 token，旧的当场失效。"""
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
    """登出：删掉这一条会话，返是否真删了（已经不存在的 token 不算错）。"""
    row = (await session.execute(
        select(AdminSession).where(AdminSession.token_hash == _hash_token(token))
    )).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def list_live_sessions(session: AsyncSession) -> list[dict]:
    """列出当前未过期的会话（id / 用户名 / 到期时间）给控制台展示。"""
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
    """强制踢下线指定会话；id 不存在返 ``False``。"""
    row = await session.get(AdminSession, session_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def change_password(
    session: AsyncSession,
    admin_id: str,
    old_password: str,
    new_password: str,
    *,
    keep_token: str = "",
) -> int:
    """自助改密：验旧口令 → 写新哈希 → 吊销除当前外的所有会话，返被吊销数。

    旧口令错 / 账号不存在都抛 ``ValueError``（路由翻译）。
    改密后其它设备的 token 全部失效（口令一旦泄露，旧会话不能继续拿用），
    只保留带 ``keep_token`` 的本次登录——改完不必重新登录。
    """
    row = await session.scalar(select(AdminUser).where(AdminUser.id == admin_id))
    if row is None:
        raise ValueError("账号不存在")
    if not verify_password(row.password_hash, old_password):
        raise ValueError("旧口令不正确")
    row.password_hash = hash_password(new_password)
    keep_hash = _hash_token(keep_token) if keep_token else ""
    sessions = (
        await session.scalars(
            select(AdminSession).where(AdminSession.admin_id == admin_id)
        )
    ).all()
    revoked = 0
    for live in sessions:
        if keep_hash and live.token_hash == keep_hash:
            continue
        await session.delete(live)
        revoked += 1
    await session.flush()
    return revoked


__all__ = [
    "login",
    "create_admin",
    "hash_password",
    "verify_password",
    "needs_password_upgrade",
    "verify_token",
    "refresh_token",
    "logout",
    "list_live_sessions",
    "revoke_session",
    "change_password",
]


async def create_admin(
    session: AsyncSession, username: str, password: str, name: str = ""
) -> AdminUser:
    """建首个超管账号（库内已有账号就拒）。

    公开的首次引导路由与 ``seed`` CLI 都用它。RBAC 后「创建账号」
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

