"""新增 rag_tasks 表（RAG 后台队列任务持久化 + 状态查询）

Revision ID: l1m2n3o4p5q6
Revises: k0l1m2n3o4p5
Create Date: 2026-09-28 11:00:00.000000

RAG 录入 / 更新 / 重建等写入类操作从「纯内存队列」升级为「任务先落库再入队」：
本表登记每个任务的生命周期（pending → running → success / failed）与结果/错误，
使前端可用 ``task_id`` 轮询进度、失败原因可追溯。跨 SQLite / Postgres 通用：
仅用 String / Text / Float / JSON，无方言特化。``status`` 建索引供列表页快速过滤。

downgrade 直接删表（任务登记是过程数据，回滚无需保留）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "l1m2n3o4p5q6"
down_revision: str | Sequence[str] | None = "k0l1m2n3o4p5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建 rag_tasks 表 + status 索引。"""
    op.create_table(
        "rag_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_type", sa.String(length=30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("knowledge_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rag_tasks_status"), "rag_tasks", ["status"], unique=False)


def downgrade() -> None:
    """删表（含索引随表一起删）。"""
    op.drop_index(op.f("ix_rag_tasks_status"), table_name="rag_tasks")
    op.drop_table("rag_tasks")
