"""SQLAlchemy ORM 模型：需要重启后仍在的状态全在这里。

分组自上而下：示例业务表（users/items）→ 知识库（docs/chunks）→
后台账号与 RBAC（admin_users/sessions/roles）→ 外部数据源与智能问数历史 →
AI 对话会话与消息 → 系统配置与模型接入（system_configs/provider_configs）→
个人博客（blog_posts）。

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
# 图片向量列（VL 嵌入模型，维度独立于文本 chunk 的 ``_VECTOR_TYPE``）。
_IMAGE_VECTOR_TYPE = Vector(get_settings().rag_image_vector_dim).with_variant(
    JSON(), "sqlite"
)


class KnowledgeDoc(Base):
    """一份知识库文档的元信息（分片在 ``KnowledgeChunk``，两边靠 ``id``/``knowledge_id`` 关联）。"""

    __tablename__ = "knowledge_docs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _uid("kb"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # 文档向量化状态机（取值见 ``app.services.rag`` 的 DocStatus）：
    # indexed=已学习（向量与当前嵌入模型一致，可检索）；pending=待重建；
    # processing=处理中；failed=上次重建报错。换嵌入模型时全库置 pending 再逐个重建。
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    # 原文全文。分片之间重叠，拼不回源文本；留着原文才能做预览/编辑/
    # 换嵌入模型重建索引，而且不会跟已入库的分片对不上。
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


class KnowledgeImage(Base):
    """一张图片及其 VL 向量，文搜图命中的就是这一行。

    与 ``KnowledgeChunk`` 分属两个向量空间（图片用 VL 嵌入模型、文本用文本
    嵌入模型），故单独建表，绝不混入 ``knowledge_chunks.vector``。一张图一行（
    图片不切块）；``image_url`` 是落盘到媒体目录后的访问地址，与文档
    ``meta["images"]`` 里的引用一致。无外键约束（与分片表同一取舍），删文档时
    服务层先删本表行。``vector`` 为 NULL 表示「有图但没可用向量」（维度不符 /
    重建到一半），文搜图会跳过它。
    """

    __tablename__ = "knowledge_images"

    id: Mapped[str] = mapped_column(
        String(80), primary_key=True, default=lambda: _uid("kbi")
    )
    knowledge_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False
    )
    image_url: Mapped[str] = mapped_column(String(500), nullable=False)
    vector: Mapped[list[float] | None] = mapped_column(
        _IMAGE_VECTOR_TYPE, default=list, nullable=True
    )
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class RagTaskRow(Base):
    """RAG 后台队列的任务登记（录入 / 更新 / 重建等写入类操作）。

    行由入队时的请求会话落库（与队列无关，先 commit），Worker 只负责推进
    ``status``：pending → running → success / failed。进程重启会丢掉内存队列，
    存量 pending 不会自动重新执行（避免半截任务重复跑），由启动日志告警提示。
    """

    __tablename__ = "rag_tasks"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: _uid("rag")
    )
    # 任务类型（TaskType 枚举值）；定义在 rag_queue，不在这里重复建模
    task_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # 入队时的请求体快照（model_dump），Worker 用它重建请求模型
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    # pending | running | success | failed
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    # 执行成功的结果（chunks / knowledge_id 等），失败时为空
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # 失败原因（异常摘要，给前端展示用）
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # 任务实际关联到的知识文档 id（ingest 类型执行前是新生成的，事后补写）
    knowledge_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    started_at: Mapped[float | None] = mapped_column(Float, default=None)
    finished_at: Mapped[float | None] = mapped_column(Float, default=None)


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

    ``username`` 唯一；``password_hash`` 是 Argon2id 摘要（存量旧值可能是
    SHA-256，登录验过即升级写回，见 ``admin_service.verify_password``）。
    权限不在这张表上，一律通过 ``AdminRoleRow`` 关联的角色推导。
    """

    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _uid("adm"))
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # Argon2id 摘要约 95 字符，64 存不下，迁移 k0l1m2n3o4p5 已拓宽到 128
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
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

    连接串（``url``）含口令，落库前用 Fernet 加密（见 ``datasource.encrypt_url``，
    存量明文仍可直接读）；服务层解密后只用于建 engine，任何对外响应都
    只回掩码串（``datasource.mask_url``），编辑时留空表示不改。
    """

    __tablename__ = "data_sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _uid("ds"))
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # sqlite | mysql | postgresql
    dialect: Mapped[str] = mapped_column(String(20), nullable=False)
    # 异步 SQLAlchemy 连接串（含口令）；Fernet 密文，仅服务层内部解密使用
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
    """模型接入：一个 provider 一行（端点 + Key + 三个模型名）。

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
    # 图片向量化（文搜图）用的 VL 嵌入模型名（如 qwen3-vl-embedding）；
    # 与文本 embedding_model 分开，只有被选为图片嵌入 provider 时参与拼请求。
    image_embedding_model: Mapped[str] = mapped_column(String(200), default="")
    rerank_model: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now, onupdate=_now)


# --- 个人博客 ---


class BlogCategory(Base):
    """一个博客分类：文章最多归属一个（个人博客量级，不引多对多关联表）。

    ``slug`` 供前台 URL 用（/category/:slug）。与 ``knowledge_id`` 同一取舍，
    文章 → 分类也不建外键，删分类前的占用校验由服务层负责。
    """

    __tablename__ = "blog_categories"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: _uid("bc")
    )
    # 展示名（唯一，方便后台下拉里认出）
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    # URL 片段（唯一）；纯中文名生成不出可用 slug 时退回随机后缀
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    # 前台展示顺序（小的在前），后台可改
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


class BlogPost(Base):
    """一篇博客文章（正文 Markdown），发布时同步进知识库做 AI 检索。

    ``knowledge_id`` 指向发布时同步出的知识文档（meta.kind == "blog"）：
    下线/删除会连带删掉它，重新发布则先删旧再重建。没外键约束（与
    ``KnowledgeChunk`` 同一取舍），一致性全在服务层 ``services.blog`` 维护。
    """

    __tablename__ = "blog_posts"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: _uid("bp")
    )
    # 前台 URL 用的短链标识（唯一）；中文标题生成不出可用 slug 时退回随机后缀
    slug: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # 留空时服务层从正文自动摘取（剥掉 Markdown 语法后截断）
    summary: Mapped[str] = mapped_column(String(500), default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 封面图：``/api/v1/upload`` 落盘后的 /media URL，可空
    cover_url: Mapped[str | None] = mapped_column(String(500), default=None)
    # 标签名列表（不设标签表：个人博客的量级 JSON 足够）
    tags: Mapped[list[Any] | None] = mapped_column(JSON, default=list)
    # 所属分类（None = 未分类）；无外键，删分类前服务层校验文章数
    category_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    # draft | published（只有已发布文章进公开接口与检索索引）
    status: Mapped[str] = mapped_column(String(20), index=True, default="draft")
    featured: Mapped[bool] = mapped_column(Boolean, default=False)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    # 发布同步出的知识文档 ID；未发布/已下线为 None
    knowledge_id: Mapped[str | None] = mapped_column(String(64), default=None)
    published_at: Mapped[float | None] = mapped_column(Float, default=None)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


# --- 给 Alembic autogenerate 用的便捷再导出 ---
__all__ = [
    "UserRow",
    "ItemRow",
    "KnowledgeDoc",
    "KnowledgeChunk",
    "KnowledgeImage",
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
    "BlogCategory",
    "BlogPost",
]
