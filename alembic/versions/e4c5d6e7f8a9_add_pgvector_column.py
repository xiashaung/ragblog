"""store knowledge chunk vectors in a real pgvector column

Revision ID: e4c5d6e7f8a9
Revises: d3b4c5d6e7f8
Create Date: 2026-09-17 17:10:00.000000

"""
from typing import Sequence, Union

from alembic import op

from app.config import get_settings
from app.db import HNSW_INDEX_NAME, ensure_pgvector_schema


# revision identifiers, used by Alembic.
revision: str = "e4c5d6e7f8a9"
down_revision: Union[str, Sequence[str], None] = "d3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 维度不符的向量装不进定长列，先原样存进这张表再置空，避免迁移丢数据
BACKUP_TABLE = "knowledge_chunks_vector_backup"


def upgrade() -> None:
    """Move ``knowledge_chunks.vector`` from JSON to ``vector(<dim>)``.

    Postgres only: SQLite (tests / lightweight dev) keeps the JSON column and
    the Python-side cosine scoring. Turning the column into a typed vector lets
    the database compute cosine distance (``<=>``) and index it with HNSW
    instead of shipping every chunk body to Python on each search.

    Rows whose stored dimension differs from ``APP_RAG_VECTOR_DIM`` cannot fit
    the column, so their original JSON is copied to
    ``knowledge_chunks_vector_backup`` and the vector is set to NULL - the
    document then shows up as "no usable vector" and needs a re-index (which
    needs the original text, stored since revision d3b4c5d6e7f8).
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 先把 pgvector 扩展建好，vector 类型才可用：否则下面 alter column ... type
    # vector(<dim>) 会在新库上报 type "vector" does not exist（本地库因启动时
    # init_models 已建过扩展而暴露不出这个顺序问题）。幂等，已存在则空转；
    # HNSW 索引仍由末尾的 ensure_pgvector_schema 负责（它要等列变成 vector 类型后才建）。
    op.execute("create extension if not exists vector")

    dim = int(get_settings().rag_vector_dim)
    op.execute(
        f"""create table if not exists {BACKUP_TABLE} (
                chunk_id varchar(80) primary key,
                dim integer,
                vector json
            )"""
    )
    op.execute(
        f"""insert into {BACKUP_TABLE} (chunk_id, dim, vector)
                select id, coalesce(json_array_length(vector), 0), vector
                  from knowledge_chunks
                 where vector is null or json_array_length(vector) <> {dim}
            on conflict (chunk_id) do nothing"""
    )
    # 模型里这一列已改成可空（NULL = 有分片但没可用向量），先跟进约束，
    # 否则下面装不进定长列的行会被 NOT NULL 拦住
    op.execute("alter table knowledge_chunks alter column vector drop not null")
    # pgvector 的文本格式就是 JSON 数组，所以 json::text::vector 直接可转
    op.execute(
        f"""alter table knowledge_chunks
                alter column vector type vector({dim})
                using (case when json_array_length("vector") = {dim}
                            then "vector"::text::vector end)"""
    )
    # 扩展与 HNSW 索引复用 init_models 那份实现，避免两处 DDL 漂移
    ensure_pgvector_schema(bind)


def downgrade() -> None:
    """Put the vectors back into a JSON column.

    The backup table is deliberately kept: dropping it would destroy the only
    copy of the vectors that were NULLed out during the upgrade.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"drop index if exists {HNSW_INDEX_NAME}")
    op.execute(
        """alter table knowledge_chunks
               alter column vector type json
               using nullif("vector"::text, '')::json"""
    )
    op.execute(
        f"""update knowledge_chunks as k
                   set vector = b.vector
                  from {BACKUP_TABLE} as b
                 where k.id = b.chunk_id"""
    )
    # 上一个修订的模型里这一列还是 NOT NULL，跟着回退
    op.execute("alter table knowledge_chunks alter column vector set not null")
