"""SQLAlchemy ORM 模型：需要重启后仍在的状态全在这里。

分组自上而下：示例业务表（users/items）→ 知识库（docs/chunks）→
后台账号与 RBAC（admin_users/sessions/roles）→ 外部数据源与智能问数历史 →
AI 对话会话与消息 → 系统配置与模型管理（system_configs/provider_configs）。

两个统一约定：

* 时间字段全用 ``Float`` 存 unix 时间戳（默认 ``_now``），跨方言不用处理时区；
* 主键用带前缀的短 uuid（``kb-xxxx`` / ``role-xxxx``），可读且不需自增协调。

API Key 体系已删（见迁移 ``a6b7c8d9e0f1``），后台统一走 AdminSession token。
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
    """行的创建/更新时间（unix 时间戳，做列默认值用）。"""
    return time.time()


def _uid(prefix: str) -> str:
    """带业务前缀的短 uuid 主键，便于从 ID 直接看出表归属。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# --- 示例业务（最早的项目样板，与新功能无关）---


class UserRow(Base):
    """``/users`` 示例接口的演示数据（不是登录账号，登录用的是 AdminUser）。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class ItemRow(Base):
    """``/items`` 示例接口的演示数据；``owner_id`` 只存值、未建外键。"""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    owner_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    created_at: Mapped[float] = mapped_column(Float, default=_now)


# --- RAG / 知识库 ---


# Postgres 用真 pgvector 列（距离在库内算、可建 HNSW 索引），SQLite 退化成
# JSON（距离在 Python 算），所以 ORM 层代码不管跑在哪边都一致。
_VECTOR_TYPE = Vector(get_settings().rag_vector_dim).with_variant(JSON(), "sqlite")


class KnowledgeDoc(Base):
    """一份知识库文档的元信息（分片在 ``KnowledgeChunk``，两边靠 ``id``/``knowledge_id`` 关联）。"""

    __tablename__ = "knowledge_docs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _uid("kb"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # 录入时实际用的 embedding provider（重建索引时会改）
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    # 原文全文。分片之间重叠，拼不回源文本；留着原文才能做预览/编辑/
    # 换 provider 重建索引，而且不会跟已入库的分片对不上。
    content: Mapped[str | None] = mapped_column(Text, default=None)
    # 列名是 metadata（SQLAlchemy 保留名，故属性叫 meta）：来源、字符数、录入方式等
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, default=dict)
    ingested_at: Mapped[float] = mapped_column(Float, default=_now)
    # 改名 / 改内容 / 重建索引时刷新（``ingested_at`` 保持不变，用于看「何时录入」）
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


class KnowledgeChunk(Base):
    """文档的一个分片及其向量，检索命中的就是这一行。

    没存 ``knowledge_id`` 外键约束（跨方言重建表代价大），关系由服务层维护；
    删文档时先删分片。词法检索直接跑在 ``text`` 列的 pg_trgm 索引上。
    """

    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    knowledge_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False
    )
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # 维度由 ``APP_RAG_VECTOR_DIM`` 决定。NULL 表示「有分片但没有可用向量」：
    # 迁移时维度不符的旧向量会被备份后置空，等文档重建索引。
    vector: Mapped[list[float] | None] = mapped_column(
        _VECTOR_TYPE, default=list, nullable=True
    )


# --- 后台账号 / 会话 / RBAC 角色 ---


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
    """能登录管理后台的账号。

    ``username`` 唯一；``password_hash`` 是 SHA-256 摘要（与
    ``AdminSession.token_hash`` 同一做法）——真正的控制靠 token 黑名单，
    这套哈希仅算凑齐可用，生产建议升级到 bcrypt/argon2。
    权限不在这张表上，一律通过 ``AdminRoleRow`` 关联的角色推导。
    """

    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _uid("adm"))
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), default="Administrator")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class AdminSession(Base):
    """登录时铸出、退出时撤销的 Bearer 会话 token。

    只存 ``token_hash``：因此即使库被拖也推不出可用 token；校验时把请求带来的
    token 先哈希再查（见 ``admin_service.verify_token``），永不存明文。
    过期靠 ``expires_at`` 比较，不是定时清理。
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
    打标，回放时提示「图片未持久化」。``seq`` 用于同一时间戳下多条消息的稳定排序。
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


# --- 系统配置（功能级别的模型/服务选择）----------------

class SystemConfigRow(Base):
    """系统级配置表（key-value，按功能模块区分）。

    取代硬编码的 .env：每个功能模块用哪个 provider（对话/嵌入/精排）
    存在这里，启动时由 ``services.provider_store`` 读进内存，改完无需重启。
    预置键（常量见 ``services/system_config.py``）：

    - chat_provider / rag_provider / embedding_provider：各功能的默认 provider
    - rerank_enabled / rerank_provider：精排开关与它用哪个 provider
    """

    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(100), primary_key=True, unique=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), default=None)
    updated_at: Mapped[float] = mapped_column(Float, default=_now, onupdate=_now)


class ProviderConfigRow(Base):
    """模型管理：一个 provider 一行（端点 + Key + 三个模型名）。

    把原先散在 .env 里的 ``APP_LLM_*`` 搬进数据库，支持后台增删改而不用改代码
    或重启；``rerank_model`` 只在被选为精排 provider 时参与拼请求。
    注意这里存的是 **明文 Key**，对外响应一律走 ``api_key_configured`` 布尔。
    缓存入口在 ``services.provider_store``（数据库优先、.env 兜底、写后 refresh）。
    """

    __tablename__ = "provider_configs"

    provider: Mapped[str] = mapped_column(String(50), primary_key=True, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), default="")
    api_key: Mapped[str] = mapped_column(String(500), default="")
    chat_model: Mapped[str] = mapped_column(String(200), default="")
    embedding_model: Mapped[str] = mapped_column(String(200), default="")
    rerank_model: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now, onupdate=_now)


# --- 给 Alembic autogenerate 用的便捷再导出 ---
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
    "SystemConfigRow",
    "ProviderConfigRow",
]
