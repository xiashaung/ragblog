"""知识库图片向量化：provider_configs 加图片模型列 + 建 knowledge_images 表

Revision ID: n3o4p5q6r7s8
Revises: m2n3o4p5q6r7
Create Date: 2026-09-28 18:00:00.000000

「文搜图」能力的数据层：

1. ``provider_configs`` 加 ``image_embedding_model`` 列——图片 VL 嵌入模型名，
   与文本 ``embedding_model`` 分开配（两者可能同 provider 但不同模型）。
2. 建 ``knowledge_images`` 表——一张图一行，存落盘 URL 与 VL 向量。向量列与
   ``knowledge_chunks`` 分属两个空间（维度取 ``rag_image_vector_dim``），故独立建表。

跨方言：Postgres 上 ``vector`` 是真 pgvector 列并建 HNSW 余弦索引；SQLite 上退化
成 JSON、打分回 Python。建表用的列类型与 ``app.models`` 完全一致（同一份
``Vector(dim).with_variant(JSON, "sqlite")``），避免两处漂移。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON

from alembic import op

from app.config import get_settings
from app.db import ensure_pgvector_schema

# revision identifiers, used by Alembic.
revision: str = "n3o4p5q6r7s8"
down_revision: str | Sequence[str] | None = "m2n3o4p5q6r7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """加图片模型列 + 建 knowledge_images 表（Postgres 上补 vector 列与 HNSW）。

    幂等：全新库由本迁移建表；若 app 启动时 ``create_all`` 已按 models 先建好
    这些对象（alembic 版本滞后于实际 schema 的开发/存量库），这里逐项跳过、
    只把版本推平，不重复建——与 ``ensure_pgvector_schema`` 的 ``IF NOT EXISTS`` 口径一致。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1) provider_configs 加 image_embedding_model（可空、默认空串，跨方言通用）
    existing_cols = {c["name"] for c in inspector.get_columns("provider_configs")}
    if "image_embedding_model" not in existing_cols:
        op.add_column(
            "provider_configs",
            sa.Column(
                "image_embedding_model",
                sa.String(length=200),
                nullable=False,
                server_default="",
            ),
        )

    # 2) Postgres 上 vector 类型要先有扩展才能建列；SQLite 走 JSON 无需扩展
    if bind.dialect.name == "postgresql":
        op.execute("create extension if not exists vector")

    if not inspector.has_table("knowledge_images"):
        # 与 app.models 同款双方言列类型：定长 vector（Postgres）/ JSON（SQLite）
        image_vector_type = Vector(get_settings().rag_image_vector_dim).with_variant(
            JSON(), "sqlite"
        )
        op.create_table(
            "knowledge_images",
            sa.Column("id", sa.String(length=80), nullable=False),
            sa.Column("knowledge_id", sa.String(length=64), nullable=False),
            sa.Column("image_url", sa.String(length=500), nullable=False),
            sa.Column("vector", image_vector_type, nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_knowledge_images_knowledge_id"),
            "knowledge_images",
            ["knowledge_id"],
            unique=False,
        )

    # 3) HNSW 索引复用 init_models 那份实现（内部对两张表分别建，幂等）
    if bind.dialect.name == "postgresql":
        ensure_pgvector_schema(bind)


def downgrade() -> None:
    """删表与索引、去掉图片模型列。"""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("drop index if exists ix_knowledge_images_vector_hnsw")
    op.drop_index(op.f("ix_knowledge_images_knowledge_id"), table_name="knowledge_images")
    op.drop_table("knowledge_images")
    with op.batch_alter_table("provider_configs") as batch_op:
        batch_op.drop_column("image_embedding_model")
