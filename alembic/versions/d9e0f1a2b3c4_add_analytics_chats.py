"""add analytics_chats table (智能问数历史)

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-09-18 18:30:00.000000

对话式问数需要持久化历史：每个后台身份（admin_id）的问答流按行存，
``result_json`` 承载完整的问数响应（SQL/数据/结论/错误），回放时直接重建卡片。
跨 SQLite / Postgres 通用：仅用 String / Text / Float。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, Sequence[str], None] = "c8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "analytics_chats",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("admin_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("question", sa.String(length=2000), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_analytics_chats_admin_id", "analytics_chats", ["admin_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_analytics_chats_admin_id", table_name="analytics_chats")
    op.drop_table("analytics_chats")
