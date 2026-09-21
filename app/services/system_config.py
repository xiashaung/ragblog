"""系统配置服务：把「各功能用哪个 provider」这类选择存在数据库里。

写在 ``system_configs`` 表（key-value），启动时由 :mod:`app.services.provider_store`
读进内存缓存，因此后台改完无需重启即生效；本模块只负责「读 + 写 + 预置默认值」，
实际取配置给调用方用的是 provider_store 的那套 getter。

与 ``config.py`` 的分工：这里只放「运行时可改」的功能级选项；结构性参数
（向量维度、超时、混合检索开关等）仍然只能改 ``.env``。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import SystemConfigRow
from . import provider_store


class SystemConfig:
    """系统配置管理器。"""

    # 预定义配置键
    KEY_CHAT_PROVIDER = "chat_provider"
    KEY_RAG_PROVIDER = "rag_provider"
    KEY_EMBEDDING_PROVIDER = "embedding_provider"
    KEY_RERANK_ENABLED = "rerank_enabled"
    KEY_RERANK_PROVIDER = "rerank_provider"

    @classmethod
    async def get(cls, session: AsyncSession, key: str, default: str | None = None) -> str | None:
        """获取配置项。"""
        stmt = select(SystemConfigRow).where(SystemConfigRow.key == key)
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        return row.value if row else default

    @classmethod
    async def set(
        cls,
        session: AsyncSession,
        key: str,
        value: str,
        description: str | None = None,
    ) -> SystemConfigRow:
        """设置或更新配置项。"""
        stmt = select(SystemConfigRow).where(SystemConfigRow.key == key)
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()

        if row:
            # 更新现有配置
            row.value = value
            if description:
                row.description = description
        else:
            # 创建新配置
            row = SystemConfigRow(key=key, value=value, description=description)
            session.add(row)

        await session.commit()
        await session.refresh(row)
        # 配置直接影响运行时（用哪个 provider 聊天/嵌入），写完立刻刷新缓存
        await provider_store.refresh(session)
        return row

    @classmethod
    async def delete(cls, session: AsyncSession, key: str) -> bool:
        """删除配置项。"""
        stmt = select(SystemConfigRow).where(SystemConfigRow.key == key)
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()

        if not row:
            return False

        await session.delete(row)
        await session.commit()
        await provider_store.refresh(session)
        return True

    @classmethod
    async def get_all(cls, session: AsyncSession) -> dict[str, str]:
        """获取所有配置项。"""
        stmt = select(SystemConfigRow)
        result = await session.execute(stmt)
        rows = result.scalars().all()
        return {row.key: row.value for row in rows}

    @classmethod
    async def init_defaults(cls, session: AsyncSession) -> None:
        """初始化默认配置（如果不存在）。

        默认值从当前 ``.env`` 推导而不是写死 ``mock``：第一次启动时行为与旧版本
        （纯 .env）一致，之后后台改过的就以数据库为准。
        """
        settings = get_settings()
        default_provider = settings.llm_default_provider
        defaults = [
            (
                cls.KEY_CHAT_PROVIDER,
                default_provider,
                "AI 对话使用的 LLM provider（如：openai, deepseek, qwen, ollama, mlx）",
            ),
            (
                cls.KEY_RAG_PROVIDER,
                default_provider,
                "知识库问答使用的 LLM provider（可独立于对话模型）",
            ),
            (
                cls.KEY_EMBEDDING_PROVIDER,
                default_provider,
                "向量嵌入使用的 embedding provider（用于知识库向量化）",
            ),
            (
                cls.KEY_RERANK_ENABLED,
                str(settings.rag_rerank_enabled).lower(),
                "是否启用重排序服务",
            ),
            (
                cls.KEY_RERANK_PROVIDER,
                default_provider,
                "重排序服务使用的 provider",
            ),
        ]

        for key, value, description in defaults:
            stmt = select(SystemConfigRow).where(SystemConfigRow.key == key)
            result = await session.execute(stmt)
            if not result.scalar_one_or_none():
                session.add(SystemConfigRow(key=key, value=value, description=description))

        await session.commit()