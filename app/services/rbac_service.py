"""RBAC 解析与管理：角色 CRUD、账号 CRUD、权限集合计算。

依赖层（``app.deps``）只需要 ``get_principal``；路由层用 ``require_permission``。
本模块负责所有会改变授权面的写操作，并把「防止自我提权 / 把自己锁在门外」
的守卫集中在这里，路由只负责抛对应 HTTP 码。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AdminRoleRow, AdminSession, AdminUser, RoleRow
from ..rbac import (
    BUILTIN_ROLES,
    SUPERADMIN_ROLE,
    WILDCARD,
    filter_valid_permissions,
    grants,
)


@dataclass(slots=True)
class Principal:
    """一个已鉴权的后台身份 + 它实际拿到的权限码集合。"""

    admin_id: str
    username: str
    name: str
    roles: list[str]
    permissions: list[str]

    def can(self, code: str) -> bool:
        """单个权限码判定，含通配（具体规则集中在 :func:`rbac.grants`）。"""
        return grants(self.permissions, code)

    @property
    def is_superadmin(self) -> bool:
        """是否持有内置超管角色——提权类守卫只看这个。"""
        return SUPERADMIN_ROLE in self.roles


def _role_id() -> str:
    """角色主键：``role_`` + 短随机位。"""
    return f"role_{uuid.uuid4().hex[:12]}"


# --- 权限解析 ---


async def permissions_for(
    session: AsyncSession, admin_id: str
) -> tuple[list[str], list[str]]:
    """(角色 code, 合并后的权限 code)；通配 ``*`` 会吞掉其余权限。"""
    rows = (
        await session.execute(
            select(RoleRow.code, RoleRow.permissions)
            .join(AdminRoleRow, AdminRoleRow.role_id == RoleRow.id)
            .where(AdminRoleRow.admin_id == admin_id)
            .order_by(RoleRow.code)
        )
    ).all()
    codes = [code for code, _perms in rows]
    merged: list[str] = []
    for _code, perms in rows:
        for perm in perms or []:
            if perm == WILDCARD:
                return codes, [WILDCARD]
            if perm not in merged:
                merged.append(perm)
    return codes, sorted(merged)


async def get_principal(session: AsyncSession, admin_id: str) -> Principal | None:
    """装配 Principal；账号不存在或被停用返回 ``None``（依赖层转 401）。"""
    admin = await session.get(AdminUser, admin_id)
    if admin is None or not admin.is_active:
        return None
    roles, permissions = await permissions_for(session, admin.id)
    return Principal(
        admin_id=admin.id,
        username=admin.username,
        name=admin.name,
        roles=roles,
        permissions=permissions,
    )


# --- 角色 ---


async def list_roles(session: AsyncSession) -> list[dict]:
    """角色列表，带成员数（前端角色页用）。"""
    members = (
        await session.execute(
            select(AdminRoleRow.role_id, func.count()).group_by(AdminRoleRow.role_id)
        )
    ).all()
    counts = {role_id: count for role_id, count in members}
    rows = (await session.scalars(select(RoleRow).order_by(RoleRow.code))).all()
    return [
        {
            "id": role.id,
            "code": role.code,
            "name": role.name,
            "description": role.description,
            "permissions": list(role.permissions or []),
            "is_builtin": role.is_builtin,
            "members": counts.get(role.id, 0),
        }
        for role in rows
    ]


async def role_payload(session: AsyncSession, role: RoleRow) -> dict:
    """单个角色的对外表示（含成员数）。"""
    members = (
        await session.scalar(
            select(func.count())
            .select_from(AdminRoleRow)
            .where(AdminRoleRow.role_id == role.id)
        )
    ) or 0
    return {
        "id": role.id,
        "code": role.code,
        "name": role.name,
        "description": role.description,
        "permissions": list(role.permissions or []),
        "is_builtin": role.is_builtin,
        "members": members,
    }


async def ensure_superadmin_membership(
    session: AsyncSession, admin_id: str
) -> RoleRow | None:
    """把账号绑到内置超管角色（首次引导专用）；角色还没建时返 ``None``。"""
    role = await session.scalar(select(RoleRow).where(RoleRow.code == SUPERADMIN_ROLE))
    if role is None:
        return None
    linked = await session.scalar(
        select(AdminRoleRow).where(
            AdminRoleRow.admin_id == admin_id, AdminRoleRow.role_id == role.id
        )
    )
    if linked is None:
        session.add(AdminRoleRow(admin_id=admin_id, role_id=role.id))
        await session.flush()
    return role


async def _role_by_id(session: AsyncSession, role_id: str) -> RoleRow | None:
    """按主键取角色，不存在返 ``None``（调用方负责翻译成业务错）。"""
    return await session.get(RoleRow, role_id)


async def _roles_by_code(session: AsyncSession, codes: list[str]) -> list[RoleRow]:
    """按 code 批量取角色；只做一次 IN 查询，不逐个回库。"""
    if not codes:
        return []
    return list((await session.scalars(select(RoleRow).where(RoleRow.code.in_(codes)))).all())


async def ensure_builtin_roles(session: AsyncSession) -> int:
    """插入 / 修复内置角色，幂等（Alembic 迁移与启动种子共用同一份定义）。

    已存在的内置角色会被重写 permissions：代码里的权限表是唯一事实源，
    升级后不用手工迁数据；只过滤非法码（``filter_valid_permissions``），不碰自定义角色。
    """
    existing = {
        role.code: role
        for role in (await session.scalars(select(RoleRow))).all()
    }
    created = 0
    for spec in BUILTIN_ROLES:
        code = str(spec["code"])
        permissions = filter_valid_permissions(spec["permissions"])
        row = existing.get(code)
        if row is None:
            session.add(
                RoleRow(
                    id=_role_id(),
                    code=code,
                    name=str(spec["name"]),
                    description=str(spec["description"]),
                    permissions=permissions,
                    is_builtin=True,
                )
            )
            created += 1
            continue
        row.permissions = permissions
        row.is_builtin = True
    await session.flush()
    return created


async def create_role(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    description: str = "",
    permissions: list[str] | None = None,
    actor: Principal | None = None,
) -> RoleRow:
    """新建角色。含提权码（``*`` / ``role:manage``）的角色只允许超管创建。"""
    code = code.strip().lower()
    if not code.replace("_", "").isalnum():
        raise ValueError("角色标识只能使用小写字母、数字、下划线")
    if await session.scalar(select(RoleRow).where(RoleRow.code == code)):
        raise ValueError(f"角色标识 {code} 已存在")
    wanted = filter_valid_permissions(permissions or [])
    _guard_wildcard(wanted)
    _guard_privilege_escalation(wanted, actor)
    role = RoleRow(
        id=_role_id(),
        code=code,
        name=name.strip() or code,
        description=description,
        permissions=wanted,
        is_builtin=False,
    )
    session.add(role)
    await session.flush()
    return role


async def update_role(
    session: AsyncSession,
    role_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    permissions: list[str] | None = None,
    actor: Principal | None = None,
) -> RoleRow:
    """改角色名 / 描述 / 权限集。

    超管角色的权限一律拒改（收掉它会把管理员自己锁在门外）；
    其他角色改权限时同样要过通配码与提权两道闸门。
    """
    role = await _role_by_id(session, role_id)
    if role is None:
        raise ValueError("角色不存在")
    if permissions is not None:
        wanted = filter_valid_permissions(permissions)
        if role.code == SUPERADMIN_ROLE:
            # 收掉超管权限会把管理员自己锁在门外（无人能再改角色）
            if wanted != [WILDCARD]:
                raise ValueError("内置超级管理员恒为全部权限，不能调整；请先创建其他管理角色")
        else:
            _guard_wildcard(wanted)
        _guard_privilege_escalation(wanted, actor, role=role)
        role.permissions = wanted
    if name is not None:
        role.name = name.strip() or role.name
    if description is not None:
        role.description = description
    await session.flush()
    return role


async def delete_role(session: AsyncSession, role_id: str) -> None:
    """删角色：内置角色不可删，还有成员时也不让删（避免默默收权）。"""
    role = await _role_by_id(session, role_id)
    if role is None:
        raise ValueError("角色不存在")
    if role.is_builtin:
        raise ValueError(f"内置角色 {role.name} 不能删除")
    members = (
        await session.scalar(
            select(func.count())
            .select_from(AdminRoleRow)
            .where(AdminRoleRow.role_id == role.id)
        )
    ) or 0
    if members:
        raise ValueError(f"还有 {members} 个账号在使用该角色，请先调整它们的角色")
    await session.delete(role)
    await session.flush()


def _guard_wildcard(wanted: list[str]) -> None:
    """通配 ``*`` 只能属于内置 superadmin。

    否则可以再造一个全权角色（可删、非超管同义词）来绕过
    「最后一个超管不能删 / 不能降权」这类保护。
    """
    if WILDCARD in wanted:
        raise ValueError("只有内置超级管理员可以持有全部权限（*），请逐条勾选权限码")


def _guard_privilege_escalation(
    wanted: list[str], actor: Principal | None, *, role: RoleRow | None = None
) -> None:
    """提权闸门：非超管不能新建 / 改动带 ``role:manage``（或 ``*``）的角色。

    否则「管理员」可以通过编辑一个自己能被赋予的角色来拿到超管权限。
    """
    grants_manage = WILDCARD in wanted or "role:manage" in wanted
    if not grants_manage:
        return
    if role is not None and role.code == SUPERADMIN_ROLE:
        return  # 超管角色本身不在此列（其权限被上游守卫拒改）
    if actor is None or not actor.is_superadmin:
        raise ValueError("只有超级管理员可以授予「管理角色」权限，请使用超级管理员账号操作")


# --- 账号 ---


async def list_accounts(session: AsyncSession) -> list[dict]:
    """账号列表 + 各自角色（按角色 code 排序，输出稳定）。"""
    rows = (
        await session.execute(
            select(AdminUser, RoleRow.code)
            .outerjoin(AdminRoleRow, AdminRoleRow.admin_id == AdminUser.id)
            .outerjoin(RoleRow, RoleRow.id == AdminRoleRow.role_id)
            .order_by(AdminUser.created_at, AdminUser.username, RoleRow.code)
        )
    ).all()
    accounts: dict[str, dict] = {}
    order: list[str] = []
    for admin, role_code in rows:
        item = accounts.get(admin.id)
        if item is None:
            item = {
                "id": admin.id,
                "username": admin.username,
                "name": admin.name,
                "is_active": admin.is_active,
                "created_at": admin.created_at,
                "roles": [],
            }
            accounts[admin.id] = item
            order.append(admin.id)
        if role_code:
            item["roles"].append(role_code)
    return [accounts[key] for key in order]


async def create_account(
    session: AsyncSession,
    *,
    username: str,
    password: str,
    name: str = "",
    role_codes: list[str] | None = None,
    actor: Principal | None = None,
) -> AdminUser:
    """新建后台账号并分配角色。"""
    from .admin_service import hash_password

    username = username.strip()
    if not username:
        raise ValueError("用户名不能为空")
    if await session.scalar(select(AdminUser).where(AdminUser.username == username)):
        raise ValueError(f"用户名 {username} 已存在")
    roles = await _resolve_roles(session, role_codes or [])
    _guard_role_assignment(roles, actor)

    admin = AdminUser(
        id=f"adm_{uuid.uuid4().hex[:12]}",
        username=username,
        password_hash=hash_password(password),
        name=name.strip() or username,
        is_active=True,
    )
    session.add(admin)
    await session.flush()
    await _write_memberships(session, admin.id, roles)
    return admin


async def update_account(
    session: AsyncSession,
    admin_id: str,
    *,
    name: str | None = None,
    is_active: bool | None = None,
    password: str | None = None,
    actor: Principal | None = None,
) -> AdminUser:
    """改名 / 启停 / 重置密码。停用与改密都会踢掉该账号的在线会话。"""
    from .admin_service import hash_password

    admin = await session.get(AdminUser, admin_id)
    if admin is None:
        raise ValueError("账号不存在")
    if is_active is False and actor is not None and actor.admin_id == admin_id:
        raise ValueError("不能停用自己的账号")
    if is_active is False and await _is_last_active_superadmin(session, admin_id):
        raise ValueError("这是最后一个可用的超级管理员，停用后无人能恢复权限配置")

    if name is not None:
        admin.name = name.strip() or admin.name
    if password:
        admin.password_hash = hash_password(password)
        await revoke_sessions(session, admin_id)
    if is_active is not None:
        admin.is_active = is_active
        if not is_active:
            await revoke_sessions(session, admin_id)
    await session.flush()
    return admin


async def set_account_roles(
    session: AsyncSession,
    admin_id: str,
    role_codes: list[str],
    *,
    actor: Principal | None = None,
) -> AdminUser:
    """覆盖式设置账号角色（分配超管 = 提权，同样过闸门）。"""
    admin = await session.get(AdminUser, admin_id)
    if admin is None:
        raise ValueError("账号不存在")
    roles = await _resolve_roles(session, role_codes)
    _guard_role_assignment(roles, actor)
    if await _is_last_active_superadmin(session, admin_id) and not any(
        role.code == SUPERADMIN_ROLE for role in roles
    ):
        raise ValueError("这是最后一个超级管理员，不能再收回其超管角色")
    await session.execute(
        delete(AdminRoleRow).where(AdminRoleRow.admin_id == admin_id)
    )
    await _write_memberships(session, admin_id, roles)
    await session.flush()
    return admin


async def delete_account(
    session: AsyncSession, admin_id: str, *, actor: Principal | None = None
) -> None:
    """删账号：不能自删，也不能删最后一个可用超管；连带清掉角色关系与会话。"""
    if actor is not None and actor.admin_id == admin_id:
        raise ValueError("不能删除自己的账号")
    admin = await session.get(AdminUser, admin_id)
    if admin is None:
        raise ValueError("账号不存在")
    if await _is_last_active_superadmin(session, admin_id):
        raise ValueError("这是最后一个超级管理员，不能删除")
    await session.execute(delete(AdminRoleRow).where(AdminRoleRow.admin_id == admin_id))
    await revoke_sessions(session, admin_id)
    await session.delete(admin)
    await session.flush()


async def revoke_sessions(session: AsyncSession, admin_id: str) -> int:
    """清掉一个账号名下的全部 token（停用 / 删除 / 重置密码时都要调）。返删除条数。"""
    result = await session.execute(
        delete(AdminSession).where(AdminSession.admin_id == admin_id)
    )
    await session.flush()
    return int(result.rowcount or 0)


async def _resolve_roles(session: AsyncSession, codes: list[str]) -> list[RoleRow]:
    """按 code 找角色，缺失就报错（避免拼错角色名静默少授权）。"""
    if not codes:
        return []
    unique = sorted({code.strip().lower() for code in codes if code and code.strip()})
    roles = await _roles_by_code(session, unique)
    found = {role.code for role in roles}
    missing = [code for code in unique if code not in found]
    if missing:
        raise ValueError(f"角色不存在：{', '.join(missing)}")
    return [role for role in roles if role.code in found]


def _guard_role_assignment(roles: list[RoleRow], actor: Principal | None) -> None:
    """非超管不能把超管 / 带提权权限的角色分配出去。"""
    if actor is not None and actor.is_superadmin:
        return
    risky = [role.code for role in roles if _role_can_manage_roles(role)]
    if risky:
        raise ValueError(
            f"角色 {', '.join(risky)} 含「管理角色」权限，只有超级管理员能分配它"
        )


def _role_can_manage_roles(role: RoleRow) -> bool:
    """该角色是否带「能再改角色/授权」的权限（通配或 ``role:manage``）。"""
    perms = role.permissions or []
    return WILDCARD in perms or "role:manage" in perms


async def _is_last_active_superadmin(session: AsyncSession, admin_id: str) -> bool:
    """判断某账号是否是「最后一个可用的超管」。"""
    super_role = await session.scalar(select(RoleRow).where(RoleRow.code == SUPERADMIN_ROLE))
    if super_role is None:
        return False
    live = (
        await session.scalars(
            select(AdminUser.id)
            .join(AdminRoleRow, AdminRoleRow.admin_id == AdminUser.id)
            .where(
                AdminRoleRow.role_id == super_role.id,
                AdminUser.is_active.is_(True),
            )
            .group_by(AdminUser.id)
        )
    ).all()
    if admin_id not in list(live):
        return False
    return len(set(live)) <= 1


async def _write_memberships(
    session: AsyncSession, admin_id: str, roles: list[RoleRow]
) -> None:
    """追加写入账号—角色关系；覆盖式设置要靠调用方先清掉旧关系。"""
    for role in roles:
        session.add(AdminRoleRow(admin_id=admin_id, role_id=role.id))
    await session.flush()


__all__ = [
    "Principal",
    "create_account",
    "create_role",
    "delete_account",
    "delete_role",
    "ensure_builtin_roles",
    "ensure_superadmin_membership",
    "get_principal",
    "list_accounts",
    "list_roles",
    "permissions_for",
    "revoke_sessions",
    "role_payload",
    "set_account_roles",
    "update_account",
    "update_role",
]
