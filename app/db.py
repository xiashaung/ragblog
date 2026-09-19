"""Async database layer.

Defaults to in-project SQLite (``sqlite+aiosqlite:///./app.db``). Postgres
works via a driver swap: ``postgresql+asyncpg://...``. Call ``init_db`` in
the app lifespan; FastAPI endpoints inject a session via ``get_db``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings

engine: AsyncEngine | None = None
session_factory: async_sessionmaker[AsyncSession] | None = None

# pgvector 余弦索引名（迁移与 init_models 共用一个名字，避免两处漂移）
HNSW_INDEX_NAME = "ix_knowledge_chunks_vector_hnsw"
# pg_trgm 词法索引名（混合检索的词法腿用它加速 similarity/word_similarity）
TRGM_INDEX_NAME = "ix_knowledge_chunks_text_trgm"
logger = logging.getLogger(__name__)

_COLUMN_TYPE_SQL = """
select format_type(a.atttypid, a.atttypmod)
  from pg_attribute a
  join pg_class c on c.oid = a.attrelid
 where c.relname = 'knowledge_chunks' and a.attname = 'vector' and a.attnum > 0
"""


def ensure_pgvector_schema(conn: Connection) -> bool:
    """Create the pgvector extension + cosine HNSW index (Postgres only).

    Called from ``init_models`` (fresh ``create_all`` path) and from the
    Alembic migration, so neither has to duplicate the DDL. Returns ``False``
    on non-Postgres dialects. Skips the index while the column is still the
    legacy ``json`` type - that is what the migration converts.
    """
    if conn.dialect.name != "postgresql":
        return False
    # SAVEPOINT 包裹：建扩展需要权限，失败不能把外层事务一并变成 aborted
    try:
        with conn.begin_nested():
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception as exc:  # noqa: BLE001 - 没权限时降级启动，不拖垮服务
        logger.warning("跳过 pgvector 扩展创建：%s", exc)
        return False
    declared = conn.execute(text(_COLUMN_TYPE_SQL)).scalar()
    if not declared or not declared.startswith("vector"):
        logger.warning(
            "knowledge_chunks.vector 当前是 %s，HNSW 索引待迁移后再建", declared
        )
        return False
    stored_dim = declared.partition("(")[2].rstrip(")")
    wanted = get_settings().rag_vector_dim
    if stored_dim.isdigit() and int(stored_dim) != wanted:
        logger.warning(
            "列维度 vector(%s) 与 APP_RAG_VECTOR_DIM=%s 不一致，对齐后重跑迁移",
            stored_dim,
            wanted,
        )
        return False
    # 1024 维在 pgvector HNSW 的维度上限内；m / ef_construction 是建图参数
    conn.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS {HNSW_INDEX_NAME} ON knowledge_chunks"
            " USING hnsw (vector vector_cosine_ops)"
            " WITH (m = 16, ef_construction = 64)"
        )
    )
    return True


def ensure_trgm_schema(conn: Connection) -> bool:
    """Create the pg_trgm extension + a GIN trigram index (Postgres only).

    Backs the lexical leg of hybrid retrieval (``word_similarity`` in
    ``rag._search_lexical_pg``). Best-effort and idempotent: on a dialect
    without it, or when the role lacks ``CREATE EXTENSION`` rights, the SAVEPOINT
    rolls back just this step and hybrid later degrades to vector-only.
    """
    if conn.dialect.name != "postgresql":
        return False
    try:
        with conn.begin_nested():
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {TRGM_INDEX_NAME} ON knowledge_chunks"
                    " USING gin (text gin_trgm_ops)"
                )
            )
    except Exception as exc:  # noqa: BLE001 - 没权限时降级启动，混合检索自动退回纯向量
        logger.warning("跳过 pg_trgm 扩展/索引创建（混合检索将退回纯向量）：%s", exc)
        return False
    return True


class Base(DeclarativeBase):
    """Declarative base for all models."""


def init_db(database_url: str | None = None, *, echo: bool | None = None) -> AsyncEngine:
    """Create (or return) the module-level engine and session factory.

    Idempotent — safe to call on every app start.
    """
    global engine, session_factory
    if engine is not None:
        return engine
    settings = get_settings()
    url = database_url or settings.database_url
    echo = echo if echo is not None else settings.db_echo
    connect_args: dict = (
        {"check_same_thread": False} if url.startswith("sqlite") else {}
    )
    engine = create_async_engine(url, echo=echo, connect_args=connect_args)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    return engine


def get_engine() -> AsyncEngine:
    if engine is None:
        raise RuntimeError("DB engine not initialised — call init_db() first")
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if session_factory is None:
        raise RuntimeError("DB engine not initialised — call init_db() first")
    return session_factory


def reset_engine() -> None:
    """Drop the module-level engine (tests / dev reload)."""
    global engine, session_factory
    engine = None
    session_factory = None


# --- External data source engines (智能问数的可选目标库) ---
# 与主库的单一 ``engine`` 不同，这里每个数据源一个独立连接池，懒建 + 缓存；
# 缓存值为 ``(归一化后的 url, engine)``，url 变了就 dispose 旧的、重建。
_source_engines: dict[str, tuple[str, AsyncEngine]] = {}


def normalize_async_url(url: str, dialect: str) -> str:
    """把用户填的同步连接串补上异步驱动（免得要他们记驱动名）。

    mysql→mysql+aiomysql，sqlite→sqlite+aiosqlite，postgresql→postgresql+asyncpg；
    已是异步驱动或不认识的串则原样返回。
    """
    url = url.strip()
    low = url.lower()
    if dialect == "mysql" and low.startswith("mysql://"):
        return "mysql+aiomysql://" + url[len("mysql://"):]
    if dialect == "mysql" and low.startswith("mysql+pymysql://"):
        return "mysql+aiomysql://" + url[len("mysql+pymysql://"):]
    if dialect == "sqlite" and low.startswith("sqlite://"):
        return "sqlite+aiosqlite://" + url[len("sqlite://"):]
    if dialect == "postgresql" and low.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


def _engine_kwargs(normalized: str) -> dict:
    # SQLite 的异步驱动需要关闭同线程检查；其他库用默认连接参数。
    if normalized.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {}


def new_probe_engine(url: str, dialect: str) -> AsyncEngine:
    """为一次性探测 / 计数建一个不缓存的引擎（调用方负责 dispose）。"""
    normalized = normalize_async_url(url, dialect)
    return create_async_engine(normalized, **_engine_kwargs(normalized))


async def get_source_engine(source_id: str, url: str, dialect: str) -> AsyncEngine:
    """返回（并按需新建 / 重建）某数据源的异步引擎。"""
    normalized = normalize_async_url(url, dialect)
    cached = _source_engines.get(source_id)
    if cached is not None and cached[0] == normalized:
        return cached[1]
    if cached is not None:  # url / dialect 变了：丢弃旧引擎
        try:
            await cached[1].dispose()
        except Exception:  # noqa: BLE001 - 丢弃失败不影响重建
            pass
    new_engine = create_async_engine(normalized, pool_pre_ping=True, **_engine_kwargs(normalized))
    _source_engines[source_id] = (normalized, new_engine)
    return new_engine


async def dispose_source_engine(source_id: str) -> None:
    """数据源被改 / 删时释放其连接池。"""
    cached = _source_engines.pop(source_id, None)
    if cached is not None:
        try:
            await cached[1].dispose()
        except Exception:  # noqa: BLE001
            pass


async def test_source_connection(url: str, dialect: str, timeout: float = 8.0) -> str:
    """临时建一个引擎探一下连通性，返回 dialect 名；失败抛异常。"""
    probe = new_probe_engine(url, dialect)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("select 1"))
            return probe.dialect.name
    finally:
        await probe.dispose()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async session (commit on success)."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_models() -> None:
    """Import all models and create any missing tables.

    For local dev and tests; in production run ``alembic upgrade head``.
    """
    import app.models  # noqa: F401  (ensures metadata is populated)

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all 只建表，pgvector 扩展与 HNSW 索引需要专有 DDL
        await conn.run_sync(ensure_pgvector_schema)
        # 混合检索的词法腿需要 pg_trgm（无则自动降级为纯向量）
        await conn.run_sync(ensure_trgm_schema)


async def dispose_engine() -> None:
    """Dispose of the engine connection pool (app shutdown)."""
    if engine is not None:
        await engine.dispose()


# Re-export for router typing convenience (e.g. `session: AsyncSession = Depends(get_db)`).
__all__ = [
    "Base",
    "HNSW_INDEX_NAME",
    "TRGM_INDEX_NAME",
    "init_db",
    "get_engine",
    "get_session_factory",
    "get_db",
    "init_models",
    "ensure_pgvector_schema",
    "ensure_trgm_schema",
    "dispose_engine",
    "reset_engine",
    "normalize_async_url",
    "new_probe_engine",
    "get_source_engine",
    "dispose_source_engine",
    "test_source_connection",
]
