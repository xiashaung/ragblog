"""知识库文档用 status 状态机取代 provider 列

Revision ID: m2n3o4p5q6r7
Revises: g6h7i8j9k0l1
Create Date: 2026-09-28 16:00:00.000000

去掉文档级 provider 跟踪，改为四态 status（indexed / pending / processing / failed）：
一个知识库只对应「当前配置的嵌入模型」，换模型时全库强制重建，搜索只命中 indexed。

跨 SQLite / Postgres 通用的三步（顺序不能乱）：

1. 先加 ``status`` 列（server_default=pending，保证 NOT NULL 有值）；
2. 回填：此刻 ``provider`` 列还在，拿它跟当前配置的 embedding_provider 比对——
   一致置 ``indexed``（复刻旧的 stale=False），不一致置 ``pending``，行为完全接续；
3. 再删 ``provider`` 列。

当前嵌入模型的取数与运行期同源：优先 system_configs 表里的 embedding_provider，
没配则回落 ``.env`` 的 llm_default_provider。downgrade 只把 provider 列加回来
（统一 mock），历史 provider 值无法还原，属有损回滚。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

from app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = "m2n3o4p5q6r7"
down_revision: str | Sequence[str] | None = "g6h7i8j9k0l1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _current_embedding_provider(bind) -> str:
    """当前配置的嵌入 provider：system_configs 优先，.env 兜底（与运行期同源）。"""
    row = bind.execute(
        text("select value from system_configs where key = 'embedding_provider'")
    ).scalar()
    return row or get_settings().llm_default_provider


def upgrade() -> None:
    """加 status → 按 provider 回填 → 删 provider → 建索引。"""
    op.add_column(
        "knowledge_docs",
        sa.Column(
            "status", sa.String(length=12), nullable=False, server_default="pending"
        ),
    )

    bind = op.get_bind()
    default = _current_embedding_provider(bind)
    # provider 列此刻仍在：与当前嵌入模型一致的算「已学习」，其余「待重建」。
    bind.execute(
        text(
            "update knowledge_docs set status = 'indexed' where provider = :default"
        ),
        {"default": default},
    )
    bind.execute(
        text(
            "update knowledge_docs set status = 'pending' "
            "where provider is null or provider <> :default"
        ),
        {"default": default},
    )

    # drop column 走 batch：SQLite 重建表删列，Postgres 退化为直接 ALTER，两端同一份代码。
    with op.batch_alter_table("knowledge_docs") as batch_op:
        batch_op.drop_column("provider")
    # 索引放在删列之后建：batch 在 SQLite 上会重建表，早建的索引会随之丢失。
    op.create_index(
        op.f("ix_knowledge_docs_status"), "knowledge_docs", ["status"], unique=False
    )


def downgrade() -> None:
    """把 provider 列加回来（统一 mock），有损：无法还原历史 provider 值。"""
    op.drop_index(op.f("ix_knowledge_docs_status"), table_name="knowledge_docs")
    with op.batch_alter_table("knowledge_docs") as batch_op:
        batch_op.drop_column("status")
        batch_op.add_column(
            sa.Column(
                "provider", sa.String(length=20), nullable=False, server_default="mock"
            )
        )
