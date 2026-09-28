"""admin_users.password_hash 列拓宽 64 → 128（口令哈希升级 Argon2id）

Revision ID: k0l1m2n3o4p5
Revises: j9k0l1m2n3o4
Create Date: 2026-09-28 10:00:00.000000

背景：登录口令哈希从裸 SHA-256（64 位十六进制）升级为 Argon2id，
新摘要约 95 字符，旧列宽存不下。存量 SHA-256 值不动——登录时由
``admin_service.login`` 校验通过后就地升级写回。

用 ``batch_alter_table``：SQLite 的 ALTER COLUMN 不支持改类型，
必须走 Alembic 的批处理重建表；Postgres 下 batch 退化为直接 ALTER，
两端同一份代码。

downgrade 反向收窄到 64。注意：库里若已有 Argon2 长哈希会被截断，
属于不可逆的数据损伤，仅作回滚演练用，生产回滚前先确认无新格式哈希。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "k0l1m2n3o4p5"
down_revision: str | Sequence[str] | None = "j9k0l1m2n3o4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """拓宽 password_hash 到 String(128)，容纳 Argon2id 摘要。"""
    with op.batch_alter_table("admin_users") as batch_op:
        batch_op.alter_column(
            "password_hash",
            existing_type=sa.String(length=64),
            type_=sa.String(length=128),
            existing_nullable=False,
        )


def downgrade() -> None:
    """收窄回 String(64)（仅回滚演练；已有 Argon2 哈希会被截断）。"""
    with op.batch_alter_table("admin_users") as batch_op:
        batch_op.alter_column(
            "password_hash",
            existing_type=sa.String(length=128),
            type_=sa.String(length=64),
            existing_nullable=False,
        )
