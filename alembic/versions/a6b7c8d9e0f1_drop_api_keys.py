"""drop api_keys + usage_logs (remove API Key feature)

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Create Date: 2026-09-18 12:00:00.000000

平台不再提供「调用本服务的 API Key」体系：相关的接口 / 服务 / 模型已全部移除，
这里删除承载它的两张表。**不可逆**：已有 Key 与本小时用量数据会丢失。
downgrade 会按初始迁移的定义把空表重建回来（数据无法恢复）。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a6b7c8d9e0f1"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f("ix_usage_logs_ts"), table_name="usage_logs")
    op.drop_index(op.f("ix_usage_logs_key_id"), table_name="usage_logs")
    op.drop_table("usage_logs")
    op.drop_table("api_keys")


def downgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("quota_per_hour", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_table(
        "usage_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("ts", sa.Float(), nullable=False),
        sa.Column("endpoint", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_usage_logs_key_id"), "usage_logs", ["key_id"], unique=False)
    op.create_index(op.f("ix_usage_logs_ts"), "usage_logs", ["ts"], unique=False)
