"""add pg_trgm extension + trigram index for hybrid retrieval

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
Create Date: 2026-09-18 15:30:00.000000

混合检索（向量 + 词法，RRF 融合）的词法腿用 pg_trgm 的三元组相似度
（``word_similarity``），它对中文无需分词也能命中关键词。这里建扩展 + GIN
trigram 索引，让词法召回能走索引而不是全表扫。Postgres only；SQLite（测试 /
轻量开发）跳过，``rag.search`` 在该后端本就退回纯向量。

DDL 复用 ``app.db.ensure_trgm_schema``，与 init_models 那份实现保持一致，
避免两处漂移。
"""

from typing import Sequence, Union

from alembic import op

from app.db import TRGM_INDEX_NAME, ensure_trgm_schema

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "a6b7c8d9e0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 建扩展 + GIN trigram 索引（幂等，复用运行期同一份 DDL）
    ensure_trgm_schema(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 只删索引；扩展留着（可能被其它查询共用，且删扩展需更高权限）
    op.execute(f"drop index if exists {TRGM_INDEX_NAME}")
