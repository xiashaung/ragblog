"""Add system configuration table.

Revision ID: g6h7i8j9k0l1
Revises: c2a3b4d5e6f7
Create Date: 2026-09-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'g6h7i8j9k0l1'
down_revision: Union[str, None] = 'c2a3b4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create system_configs table
    op.create_table(
        'system_configs',
        sa.Column('key', sa.String(100), nullable=False),
        sa.Column('value', sa.Text, nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_index(op.f('ix_system_configs_key'), 'system_configs', ['key'], unique=True)
    
    # Insert default configurations
    op.execute("""
        INSERT INTO system_configs (key, value, description, updated_at) VALUES
        ('chat_provider', 'mock', 'AI 对话使用的 LLM provider（如：openai, deepseek, qwen, ollama, mlx）', strftime('%s', 'now')),
        ('rag_provider', 'mock', '知识库问答使用的 LLM provider（可独立于对话模型）', strftime('%s', 'now')),
        ('embedding_provider', 'mock', '向量嵌入使用的 embedding provider（用于知识库向量化）', strftime('%s', 'now')),
        ('rerank_enabled', 'false', '是否启用重排序服务', strftime('%s', 'now')),
        ('rerank_provider', 'mock', '重排序服务使用的 provider', strftime('%s', 'now'))
    """)


def downgrade() -> None:
    op.drop_index(op.f('ix_system_configs_key'), table_name='system_configs')
    op.drop_table('system_configs')
