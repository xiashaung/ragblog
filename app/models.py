"""SQLAlchemy ORM models.

All state that should survive restarts lives here:
users, items, RAG knowledge, API keys, and per-key usage logs.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .config import get_settings
from .db import Base


def _now() -> float:
    return time.time()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# --- Business ---


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class ItemRow(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    owner_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    created_at: Mapped[float] = mapped_column(Float, default=_now)


# --- RAG / knowledge base ---


# Postgres 用真 pgvector 列（距离在库内算、可建 HNSW 索引），SQLite 退化成
# JSON（距离在 Python 算），所以 ORM 层代码不管跑在哪边都一致。
_VECTOR_TYPE = Vector(get_settings().rag_vector_dim).with_variant(JSON(), "sqlite")


class KnowledgeDoc(Base):
    __tablename__ = "knowledge_docs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _uid("kb"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    # Full original text. Chunks overlap, so they cannot be re-joined into the
    # source; keeping the raw text is what makes preview / edit / re-index
    # (with another embedding provider) possible without drift.
    content: Mapped[str | None] = mapped_column(Text, default=None)
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, default=dict)
    ingested_at: Mapped[float] = mapped_column(Float, default=_now)
    # Bumped on rename / content change / re-index (``ingested_at`` stays put).
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    knowledge_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False
    )
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # 维度由 ``APP_RAG_VECTOR_DIM`` 决定。NULL 表示“有分片但没有可用向量”：
    # 迁移时维度不符的旧向量会被备份后置空，等文档重建索引。
    vector: Mapped[list[float] | None] = mapped_column(
        _VECTOR_TYPE, default=list, nullable=True
    )


# --- Backend / admin account + sessions (admin dashboard) ---


class RoleRow(Base):
    """RBAC 角色。

    角色存在数据库里（可在后台增删改），但权限码本身由 ``app/rbac.py``
    定义 - 否则路由上的 ``require_permission(...)`` 会被数据库里改字符串绕过。
    ``permissions == ["*"]`` 表示全权（仅用于内置 superadmin）。
    """

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _uid("role"))
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(200), default="")
    permissions: Mapped[list[Any] | None] = mapped_column(JSON, default=list)
    # 内置角色不可删、code 不可改（由迁移负责种子）
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class AdminRoleRow(Base):
    """账号 : 角色 = 多对多。

    没用 SQLAlchemy relationship：全异步下隐式加载会 DetachedInstanceError，
    权限解析统一走 rbac_service 里的显式 JOIN 查询。
    """

    __tablename__ = "admin_user_roles"

    admin_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class AdminUser(Base):
    """An account that can sign into the admin dashboard.

    ``username`` is unique; ``password_hash`` is a SHA-256 digest
    (mirrors ``AdminSession.token_hash`` — adequate alongside token blacklists,
    which is the real control here; upgrade to bcrypt/argon2 in prod).
    """

    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _uid("adm"))
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), default="Administrator")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class AdminSession(Base):
    """A bearer session token minted at login and revoked at logout.

    Only ``token_hash`` is stored, so an extracted ``Authorization``
    header value is hashed on read before lookup.
    """

    __tablename__ = "admin_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _uid("ses"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    admin_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


# --- External data sources (智能问数可切换分析的目标库) ---


class DataSourceRow(Base):
    """一条外部数据源配置。

    连接串（``url``）含口令，只在服务内部用于建 engine；任何对外响应都
    只回掩码串（见 ``datasource.mask_url``），编辑时留空表示不改。
    """

    __tablename__ = "data_sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _uid("ds"))
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # sqlite | mysql | postgresql
    dialect: Mapped[str] = mapped_column(String(20), nullable=False)
    # 异步 SQLAlchemy 连接串（含口令，仅内部用）
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # 是否强制只读（PG/MySQL 走只读事务；SQLite 靠不回滚兼底）
    readonly: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


class AnalyticsChatRow(Base):
    """智能问数的历史问答（按后台身份归档，仅本人可见）。

    ``result_json`` 是整份 AnalyticsQueryResponse 的序列化，回放时直接重建卡片；
    只存问句与结果，不含任何连接串信息（source 只存 key）。
    """

    __tablename__ = "analytics_chats"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: _uid("ac")
    )
    # 归属的后台身份（Principal.admin_id），历史只对本人可见
    admin_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="main")
    question: Mapped[str] = mapped_column(String(2000), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class ChatSessionRow(Base):
    """AI 对话的会话（按后台身份归档，仅本人可见）。

    会话只存标题与时间，消息正文在 ``ChatMessageRow``；删会话时由服务层
    显式删子记录，不依赖外键级联（跨 SQLite / Postgres 行为不一致）。
    """

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: _uid("cs")
    )
    # 归属的后台身份（Principal.admin_id），会话只对本人可见
    admin_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


class ChatMessageRow(Base):
    """会话里的一条消息（一行一条，不按轮打包）。

    ``images_json`` 存随消息附带的图片 data URL 数组——多轮上下文要原样重建才能
    继续让视觉模型看到图；超过存储上限时服务层只丢图保留正文并在 ``meta_json``
    打标，回放时提示“图片未持久化”。``seq`` 用于同一时间戳下多条消息的稳定排序。
    """

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: _uid("cm")
    )
    session_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    images_json: Mapped[str] = mapped_column(Text, default="[]")
    # {"usage": {...}, "images_dropped": n} —— 用量等展示性元数据，不再拆列
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


# --- Convenience re-exports for Alembic autogenerate ---
__all__ = [
    "UserRow",
    "ItemRow",
    "KnowledgeDoc",
    "KnowledgeChunk",
    "RoleRow",
    "AdminRoleRow",
    "AdminUser",
    "AdminSession",
    "DataSourceRow",
    "AnalyticsChatRow",
    "ChatSessionRow",
    "ChatMessageRow",
]
