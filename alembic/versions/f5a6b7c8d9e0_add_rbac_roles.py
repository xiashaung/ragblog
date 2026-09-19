"""add RBAC roles + account/role membership

Revision ID: f5a6b7c8d9e0
Revises: e4c5d6e7f8a9
Create Date: 2026-09-18 10:20:00.000000

Seed 语义：
- 内置角色由 ``app.rbac.BUILTIN_ROLES`` 决定（代码是唯一来源），这里只做落库；
- 迁移前系统只有「是不是 admin」一个布尔，所有现存 admin_users 实际都拥有全部
  管理权限，因此一律绑定 ``superadmin``，避免升级后有人被降权。
"""

from __future__ import annotations

import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.rbac import BUILTIN_ROLES, SUPERADMIN_ROLE

# revision identifiers, used by Alembic.
revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "e4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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

def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=True),
        sa.Column("is_builtin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_roles_code"),
    )
    op.create_table(
        "admin_user_roles",
        sa.Column("admin_id", sa.String(length=64), nullable=False),
        sa.Column("role_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["admin_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("admin_id", "role_id"),
    )
    op.create_index(
        op.f("ix_admin_user_roles_role_id"), "admin_user_roles", ["role_id"], unique=False
    )

    bind = op.get_bind()
    created_at = time.time()
    for index, spec in enumerate(BUILTIN_ROLES):
        bind.execute(
            roles.insert().values(
                id=f"role_builtin_{spec['code']}",
                code=spec["code"],
                name=spec["name"],
                description=spec["description"],
                permissions=list(spec["permissions"]),
                is_builtin=True,
                # 微增量，保证列表顺序与定义顺序一致
                created_at=created_at + index,
            )
        )
    # 现存账号 → 超管（等价于升级前的「唯一管理员」能力）
    bind.execute(
        sa.text(
            "insert into admin_user_roles (admin_id, role_id)"
            " select u.id, r.id from admin_users u"
            " cross join roles r"
            " where r.code = :super"
            " and not exists (select 1 from admin_user_roles m"
            " where m.admin_id = u.id)"
        ),
        {"super": SUPERADMIN_ROLE},
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_admin_user_roles_role_id"), table_name="admin_user_roles"
    )
    op.drop_table("admin_user_roles")
    op.drop_table("roles")
