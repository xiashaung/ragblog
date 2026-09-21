"""异步数据库层：主库引擎、会话依赖、以及智能问数的外部数据源引擎。

默认用项目内的 SQLite（``sqlite+aiosqlite:///./app.db``）；换到 Postgres
只需改驱动串：``postgresql+asyncpg://...``。两条使用路径：

    启动：lifespan 调 ``init_db()`` 建引擎 → ``init_models()`` 建表并补专有 DDL
    请求：路由用 ``Depends(get_db)`` 注入会话，成功自动 commit、异常回滚

两类引擎别混淆：上面的 ``engine`` 是全应用唯一的主库；下面的
``_source_engines`` 是「智能问数」可选的外部库，每个数据源一个连接池、懒建带缓存。
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
    """建 pgvector 扩展 + 余弦 HNSW 索引（仅 Postgres）。

    由 ``init_models``（全新 ``create_all`` 路径）和 Alembic 迁移两边同调，
    DDL 只写这一份。非 Postgres 直接返 ``False``；列还是旧的 ``json`` 类型时
    跳过建索引（转换由迁移负责）；维度与 ``APP_RAG_VECTOR_DIM`` 不一致时也跳过，
    只记一条警告——这种情况下旧向量已不可信，需要重建索引而不是盲建。
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
    """建 pg_trgm 扩展 + 三元组 GIN 索引（仅 Postgres）。

    给混合检索的词法腿加速（``rag._search_lexical_pg`` 里的 word_similarity）。
    best-effort 且幂等：方言不支持、或当前角色没 ``CREATE EXTENSION`` 权限时，
    SAVEPOINT 只回滚这一步，其余照常提交，上层会退到纯向量检索。
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
    """所有 ORM 模型的声明式基类（``app/models.py`` 继承它）。"""


def init_db(database_url: str | None = None, *, echo: bool | None = None) -> AsyncEngine:
    """建（或复用）模块级的引擎与会话工厂。

    幂等：已有引擎时直接返回，所以每次应用启动、每个测试 fixture 都能安全调用。
    ``expire_on_commit=False`` 是有意的：commit 后路由还要把 ORM 对象转成
    pydantic 响应，若对象过期就会在异步会话里触发隐式重查并报错。
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
    """取主库引擎；未初始化就报错（提醒调用方先走 ``init_db``）。"""
    if engine is None:
        raise RuntimeError("DB engine not initialised — call init_db() first")
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """取会话工厂：给后台任务等拿不到 ``Depends`` 的地方自己开事务。"""
    if session_factory is None:
        raise RuntimeError("DB engine not initialised — call init_db() first")
    return session_factory


def reset_engine() -> None:
    """丢掉模块级引擎（测试之间隔离 / 开发重载用，不 dispose 连接池）。"""
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
    """按驱动类型给 ``create_async_engine`` 补参数。"""
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
    """FastAPI 依赖：交出一个异步会话，成功收尾 commit、异常 rollback。

    路由里写了 flush 不代表落库，真正的提交在这一层；因此需要「提交后还能
    读字段」的地方，都依赖 ``init_db`` 里的 ``expire_on_commit=False``。
    后台任务不在请求作用域里，必须改用 ``get_session_factory()()`` 自己开会话。
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_models() -> None:
    """导入全部模型并建缺失的表（仅本地开发与测试；生产跑 ``alembic upgrade head``）。

    先 ``run_sync(Base.metadata.create_all)`` 建普通表，再补两个 create_all
    做不了的专有 DDL（pgvector/HNSW、pg_trgm），两个都是幂等的。
    """
    import app.models  # noqa: F401  (ensures metadata is populated)

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all 只建表，pgvector 扩展与 HNSW 索引需要专有 DDL
        await conn.run_sync(ensure_pgvector_schema)
        # 混合检索的词法腿需要 pg_trgm（无则自动降级为纯向量）
        await conn.run_sync(ensure_trgm_schema)


async def dispose_engine() -> None:
    """释放主库连接池（应用关闭时调）。"""
    if engine is not None:
        await engine.dispose()


# 给路由做类型标注用的再导出（如 `session: AsyncSession = Depends(get_db)`）。
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
