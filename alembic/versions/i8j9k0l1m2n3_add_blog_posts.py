"""博客文章表 + 内置角色补发博客权限

Revision ID: i8j9k0l1m2n3
Revises: h7i8j9k0l1m2
Create Date: 2026-09-22 10:00:00.000000

两件事一份迁移（都以代码为准，跨 SQLite / Postgres 通用）：

- 建 ``blog_posts``：个人博客的文章行。正文 Markdown、标签用 JSON 列
  （与 ``roles.permissions`` 同一做法，不引方言特化类型）、时间戳全 Float。
  ``knowledge_id`` 指向发布时同步出的知识文档，无外键约束（与
  ``knowledge_chunks`` 同一取舍，关系由服务层维护）。
- 内置角色权限重刷：``app/rbac.py::BUILTIN_ROLES`` 给 admin 补发了
  ``blog:read``/``blog:manage``、viewer 补发 ``blog:read``，但 seed 只在
  首次引导时执行，已建号的库要靠这条迁移落数据（h7i8j9k0l1m2 同款套路）。

downgrade 删表，并只回收本次预发的 blog 码，不承诺还原历史漂移。
"""

from __future__ import annotations

import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.rbac import BUILTIN_ROLES, Permission, filter_valid_permissions

# revision identifiers, used by Alembic.
revision: str = "i8j9k0l1m2n3"
down_revision: Union[str, Sequence[str], None] = "h7i8j9k0l1m2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: downgrade 要回收的码（upgrade 的结果由 ``BUILTIN_ROLES`` 决定，这里不重复一份定义）
GRANTED: dict[str, list[str]] = {
    "admin": [Permission.BLOG_READ, Permission.BLOG_MANAGE],
    "viewer": [Permission.BLOG_READ],
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

    与 h7i8j9k0l1m2 同构：迁移跑在同步连接上，不能复用异步的
    ``ensure_builtin_roles``，按同一份定义手写等价的同步逻辑。
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
    op.create_table(
        "blog_posts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("slug", sa.String(length=150), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("cover_url", sa.String(length=500), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("featured", sa.Boolean(), nullable=False),
        sa.Column("view_count", sa.Integer(), nullable=False),
        sa.Column("knowledge_id", sa.String(length=64), nullable=True),
        sa.Column("published_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_blog_posts_slug"),
    )
    op.create_index("ix_blog_posts_status", "blog_posts", ["status"])
    _sync_builtin_roles()


def downgrade() -> None:
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
    op.drop_index("ix_blog_posts_status", table_name="blog_posts")
    op.drop_table("blog_posts")
