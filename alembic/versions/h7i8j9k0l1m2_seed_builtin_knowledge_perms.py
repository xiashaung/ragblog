"""内置角色权限集与代码对齐 + 预发知识库权限码

Revision ID: h7i8j9k0l1m2
Revises: e0f1a2b3c4d5
Create Date: 2026-09-21 10:00:00.000000

为什么需要这条迁移：

- 权限码目录和内置角色定义都在代码里（``app/rbac.py``），但角色数据是落库的；
  按代码重写内置角色权限的 ``ensure_builtin_roles`` 只在首次引导 —— ``/admin/onboard``
  与 ``bootstrap seed`` —— 时执行，已经建过号的库不会自动重刷。所以「给 admin / viewer
  预发 ``knowledge:*``」必须靠迁移落到现存数据上，否则新库和老库的行为会悄悄分叉。
- 顺带治漂移：以代码为准整份重刷，老库里 API Key 时代留下的 ``key:read`` / ``key:manage``
  会被清掉，后来才加进 admin 的 ``analytics:read`` / ``datasource:read`` 也会补齐。
- ``system:*`` 有意不预发：模型接入的读取接口会把 API Key 明文回显。
- downgrade 只回收本次预发的 knowledge 码，不承诺还原迁移前的历史漂移。
"""

from __future__ import annotations

import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.rbac import BUILTIN_ROLES, Permission, filter_valid_permissions

# revision identifiers, used by Alembic.
revision: str = "h7i8j9k0l1m2"
down_revision: Union[str, Sequence[str], None] = "e0f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: downgrade 要回收的码（upgrade 的结果由 ``BUILTIN_ROLES`` 决定，这里不重复一份定义）
GRANTED: dict[str, list[str]] = {
    "admin": [Permission.KNOWLEDGE_READ, Permission.KNOWLEDGE_MANAGE],
    "viewer": [Permission.KNOWLEDGE_READ],
}


roles = sa.table(
    "roles",
    sa.column("id"),
    sa.column("code"),
    sa.column("name"),
    sa.column("description"),
    sa.column("permissions", sa.JSON()),
    sa.column("is_builtin"),
    sa.column("created_at"),
)


def _sync_builtin_roles() -> None:
    """把内置角色的权限集重刷成 ``BUILTIN_ROLES``（缺失的补建，自定义角色不碰）。

    迁移跑在同步连接上，不能复用异步的 ``ensure_builtin_roles``，因此这里按同一份
    定义（``filter_valid_permissions`` 过滤 + 目录排序）手写等价的同步逻辑。
    """
    bind = op.get_bind()
    rows = bind.execute(sa.select(roles.c.id, roles.c.code, roles.c.permissions)).all()
    existing = {row.code: row for row in rows}
    created_at = time.time()
    for index, spec in enumerate(BUILTIN_ROLES):
        code = str(spec["code"])
        permissions = filter_valid_permissions(spec["permissions"])
        row = existing.get(code)
        if row is None:
            bind.execute(
                roles.insert().values(
                    id=f"role_builtin_{code}",
                    code=code,
                    name=str(spec["name"]),
                    description=str(spec["description"]),
                    permissions=permissions,
                    is_builtin=True,
                    created_at=created_at + index,
                )
            )
            continue
        if list(row.permissions or []) == permissions:
            continue  # 已经一致，不写无谓的更新
        bind.execute(
            sa.update(roles).where(roles.c.id == row.id).values(permissions=permissions)
        )


def upgrade() -> None:
    _sync_builtin_roles()


def downgrade() -> None:
    """回收本次预发的 knowledge 码，其余原样保留。"""
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(roles.c.id, roles.c.code, roles.c.permissions).where(
            roles.c.code.in_(list(GRANTED))
        )
    ).all()
    for row in rows:
        current = list(row.permissions or [])
        merged = [code for code in current if code not in GRANTED[row.code]]
        if merged == current:
            continue
        bind.execute(
            sa.update(roles).where(roles.c.id == row.id).values(permissions=merged)
        )
