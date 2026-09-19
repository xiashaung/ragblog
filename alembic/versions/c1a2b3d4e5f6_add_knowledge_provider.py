"""add provider column to knowledge_docs

Revision ID: c1a2b3d4e5f6
Revises: bd86ceb0dd81
Create Date: 2026-09-15 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c1a2b3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "bd86ceb0dd81"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the ``provider`` column to knowledge_docs.

    Tracks which LLM provider generated the document's chunk vectors, so
    searches can be scoped to a single provider (vectors from different
    providers / models are not comparable).
    """
    op.add_column(
        "knowledge_docs",
        sa.Column("provider", sa.String(length=20), nullable=False, server_default="mock"),
    )


def downgrade() -> None:
    op.drop_column("knowledge_docs", "provider")
