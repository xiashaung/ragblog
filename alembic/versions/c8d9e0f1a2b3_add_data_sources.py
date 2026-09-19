"""add external data_sources table (智能问数可选目标库)

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-09-18 16:10:00.000000

智能问数支持切换分析多个数据库：本表登记外部数据源（MySQL / SQLite / Postgres）。
``url`` 含连接口令，仅服务内部用于建引擎，任何对外响应都只回掩码串。
「本应用主库」是合成源（不落本表），因此这里无需 seed，只建表。跨 SQLite /
Postgres 通用：仅用 String / Text / Boolean / Float 这些两端都支持的类型。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "data_sources",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("dialect", sa.String(length=20), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("readonly", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_data_sources_name"),
    )


def downgrade() -> None:
    op.drop_table("data_sources")
