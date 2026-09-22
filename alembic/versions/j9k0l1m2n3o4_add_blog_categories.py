"""博客分类表 + 文章挂分类字段

Revision ID: j9k0l1m2n3o4
Revises: i8j9k0l1m2n3
Create Date: 2026-09-22 12:00:00.000000

两件事（跨 SQLite / Postgres 通用，无方言特化 SQL）：

- 建 ``blog_categories``：博客分类（name/slug 唯一 + sort_order 排序），
  并给 ``blog_posts`` 补 ``category_id``（可空、带索引，无外键约束——
  与 ``knowledge_id`` 同一取舍，删分类前的占用校验在服务层做）。
- 不动角色权限：分类管理复用 ``blog:read`` / ``blog:manage``，
  上一条迁移（i8j9k0l1m2n3）已经把码发过了。

downgrade 反向：删列、删表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "j9k0l1m2n3o4"
down_revision: str | Sequence[str] | None = "i8j9k0l1m2n3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blog_categories",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_blog_categories_name"),
        sa.UniqueConstraint("slug", name="uq_blog_categories_slug"),
    )
    op.add_column("blog_posts", sa.Column("category_id", sa.String(length=64), nullable=True))
    op.create_index("ix_blog_posts_category_id", "blog_posts", ["category_id"])


def downgrade() -> None:
    op.drop_index("ix_blog_posts_category_id", table_name="blog_posts")
    op.drop_column("blog_posts", "category_id")
    op.drop_table("blog_categories")
