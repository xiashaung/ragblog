"""Add system configuration table.

Revision ID: g6h7i8j9k0l1
Revises: l1m2n3o4p5q6
Create Date: 2026-09-19 12:00:00.000000

说明（2026-09 收敛双 head 时重挂）：本迁移原本孤挂在 c2a3b4d5e6f7 上形成
第二个 head，且从未在任何库上真正 apply 过——system_configs 表一直是应用
启动 ``create_all`` 兜底建的。现在把它接回主线尾部（down_revision 改为
l1m2n3o4p5q6），历史迁移文件不删不合并，保持可重放。

两处刻意的改写：

- 建表幂等（inspect 存在即跳过）：存量库表已由 create_all 建好，直接
  ``upgrade head`` 撞表会炸；新库则真走这里建表。
- 删掉了原来的默认配置 INSERT：旧语句用了 SQLite 专有的 ``strftime``，
  在 Postgres 上必炸；且这些种子值由应用启动的
  ``SystemConfig.init_defaults()`` 统一负责（默认值从 .env 推导，比迁移里
  写死 'mock' 更对）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'g6h7i8j9k0l1'
down_revision: Union[str, None] = 'l1m2n3o4p5q6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    """当前库里是否已有该表（幂等判定的唯一依据）。"""
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _table_exists('system_configs'):
        return  # 存量库（create_all 兜底建过）：什么都不用做
    op.create_table(
        'system_configs',
        sa.Column('key', sa.String(100), nullable=False),
        sa.Column('value', sa.Text, nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_index(op.f('ix_system_configs_key'), 'system_configs', ['key'], unique=True)


def downgrade() -> None:
    if not _table_exists('system_configs'):
        return
    op.drop_index(op.f('ix_system_configs_key'), table_name='system_configs')
    op.drop_table('system_configs')
