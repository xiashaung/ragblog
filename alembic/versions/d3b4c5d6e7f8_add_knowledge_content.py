"""add content and updated_at columns to knowledge_docs

Revision ID: d3b4c5d6e7f8
Revises: c2a3b4d5e6f7
Create Date: 2026-09-17 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "c2a3b4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Store the original text plus a modification timestamp per document.

    ``content`` powers the knowledge-base management UI (preview, edit, and
    re-index with a different embedding provider). Chunks overlap, so the
    source text cannot be rebuilt from them. Existing rows keep ``content``
    NULL until they are re-ingested; ``updated_at`` is backfilled from
    ``ingested_at``.
    """
    op.add_column(
        "knowledge_docs",
        sa.Column("content", sa.Text(), nullable=True),
    )
    op.add_column(
        "knowledge_docs",
        sa.Column("updated_at", sa.Float(), nullable=True),
    )
    op.execute(
        "UPDATE knowledge_docs SET updated_at = ingested_at WHERE updated_at IS NULL"
    )
    op.alter_column(
        "knowledge_docs",
        "updated_at",
        existing_type=sa.Float(),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("knowledge_docs", "updated_at")
    op.drop_column("knowledge_docs", "content")
