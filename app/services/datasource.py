"""外部数据源管理：CRUD + 连接串加密落盘 + 连通性测试。

连接串含口令，本模块负责：
- 写入时把用户填的同步串 / 裸路径补成可用的异步连接串，再用 Fernet 加密落库；
- 读出时只给掩码串（口令打码），原文永不进响应；解密统一走 :func:`decrypt_url`
  （存量明文原样通过，两种格式共存）；
- 改 / 删时释放对应的连接池（``db.dispose_source_engine``）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
import uuid

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db
from ..config import get_settings
from ..models import DataSourceRow
from ..schemas.analytics import AnalyticsSource
from ..schemas.datasource import (
    DATA_SOURCE_DIALECTS,
    DataSourceOut,
    DataSourceTestResult,
)

logger = logging.getLogger(__name__)


def _new_id() -> str:
    """数据源主键：``ds_`` + 短随机位。"""
    return f"ds_{uuid.uuid4().hex[:12]}"


def _validate_dialect(dialect: str) -> str:
    """白名单校验数据库类型，顺带归一（去空白 + 转小写）。"""
    dialect = (dialect or "").strip().lower()
    if dialect not in DATA_SOURCE_DIALECTS:
        raise ValueError(
            f"不支持的数据库类型：{dialect}（可选 {'/'.join(DATA_SOURCE_DIALECTS)}）"
        )
    return dialect


def _prepare_url(dialect: str, url: str) -> str:
    """写入前归一：裸的 SQLite 文件路径补成连接串，再统一补异步驱动。"""
    url = (url or "").strip()
    if not url:
        raise ValueError("连接串不能为空")
    if dialect == "sqlite" and "://" not in url:
        # 用户直接粘了个文件路径 → 拼成 sqlite 连接串（绝对路径需四斜杠）
        url = "sqlite://" + url
    return db.normalize_async_url(url, dialect)


def _fernet() -> Fernet:
    """由配置派生 Fernet 密钥（32 字节 URL-safe base64）。

    优先 ``APP_DATASOURCE_SECRET_KEY``；没配则从引导密钥派生确定值，
    保证开箱即用就加密落盘。用模块属性做缓存：测试里换 settings 时记得
    一并重置它（或直接走 fixture 的 settings 补丁）。
    """
    settings = get_settings()
    secret = settings.datasource_secret_key or settings.admin_api_key or "datasource-url"
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


_FERNET_PREFIX = b"gAAAAA"  # Fernet token 的固定开头（version+时间戳的 base64 前缀）


def encrypt_url(plain: str) -> str:
    """连接串 → Fernet 密文（落库形态）。"""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_url(stored: str) -> str:
    """库里存的值 → 明文连接串；兼容存量明文（非密文格式直接原样过）。

    看着像密文却解不出（换过密钥/数据损坏）时必须报错而不是静默返回密文——
    密文当连接串去连库只会得到一个难以定位的「连不上」。
    """
    if not stored or not stored.startswith(_FERNET_PREFIX.decode()):
        return stored  # 存量明文
    try:
        return _fernet().decrypt(stored.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "数据源连接串无法解密：检查 APP_DATASOURCE_SECRET_KEY 是否换过，"
            "否则需重新录入该数据源"
        ) from exc


def mask_url(url: str) -> str:
    """把连接串里的口令打码；解析失败则原样正则兜底，绝不回明文口令。"""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - 非法串也要能安全展示
        return "***"


def to_out(row: DataSourceRow) -> DataSourceOut:
    """ORM 行 → 对外模型；只给 ``url_masked``，明文连接串永不外泄。"""
    return DataSourceOut(
        id=row.id,
        name=row.name,
        dialect=row.dialect,
        url_masked=mask_url(decrypt_url(row.url)),
        readonly=row.readonly,
        enabled=row.enabled,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _unique_name(
    session: AsyncSession, name: str, *, exclude_id: str | None = None
) -> None:
    """名称唯一性校验；``exclude_id`` 用于「改自己不算重名」。"""
    existing = await session.scalar(select(DataSourceRow).where(DataSourceRow.name == name))
    if existing is not None and existing.id != exclude_id:
        raise ValueError(f"数据源名称已存在：{name}")


async def list_sources(session: AsyncSession) -> list[DataSourceOut]:
    """管理页列表：全部数据源（含已禁用的），按创建时间。"""
    rows = (
        await session.scalars(select(DataSourceRow).order_by(DataSourceRow.created_at))
    ).all()
    return [to_out(row) for row in rows]


async def selectable_sources(session: AsyncSession) -> list[AnalyticsSource]:
    """问数页下拉：内置主库 + 所有启用的外部源（只给选择用的最小信息）。"""
    items: list[AnalyticsSource] = [
        AnalyticsSource(key="main", name="本应用主库", dialect=get_main_dialect(), builtin=True)
    ]
    rows = (
        await session.scalars(
            select(DataSourceRow)
            .where(DataSourceRow.enabled.is_(True))
            .order_by(DataSourceRow.created_at)
        )
    ).all()
    items.extend(
        AnalyticsSource(key=row.id, name=row.name, dialect=row.dialect, builtin=False)
        for row in rows
    )
    return items


def get_main_dialect() -> str:
    """主库方言，供问数页下拉把「内置主库」也标上类型。"""
    return db.get_engine().dialect.name


async def create(
    session: AsyncSession,
    *,
    name: str,
    dialect: str,
    url: str,
    readonly: bool = True,
    enabled: bool = True,
    description: str = "",
) -> DataSourceRow:
    """新建数据源：校验名称 → 校验方言 → 归一连接串 → 落库（连接池首次用时才建）。"""
    name = name.strip()
    if not name:
        raise ValueError("数据源名称不能为空")
    dialect = _validate_dialect(dialect)
    await _unique_name(session, name)
    prepared = _prepare_url(dialect, url)
    now = time.time()
    row = DataSourceRow(
        id=_new_id(),
        name=name,
        dialect=dialect,
        url=encrypt_url(prepared),
        readonly=readonly,
        enabled=enabled,
        description=description.strip(),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def update(
    session: AsyncSession,
    source_id: str,
    *,
    name: str | None = None,
    dialect: str | None = None,
    url: str | None = None,
    readonly: bool | None = None,
    enabled: bool | None = None,
    description: str | None = None,
) -> DataSourceRow:
    """部分更新：``None`` 表示「没动」；URL 传空串也不覆盖（避免掩码回显后被清空）。"""
    row = await session.get(DataSourceRow, source_id)
    if row is None:
        raise ValueError("数据源不存在")
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("数据源名称不能为空")
        if name != row.name:
            await _unique_name(session, name, exclude_id=row.id)
        row.name = name
    if dialect is not None:
        row.dialect = _validate_dialect(dialect)
    if url is not None and url.strip():
        row.url = encrypt_url(_prepare_url(row.dialect, url))
    if readonly is not None:
        row.readonly = readonly
    if enabled is not None:
        row.enabled = enabled
    if description is not None:
        row.description = description.strip()
    row.updated_at = time.time()
    await session.flush()
    # 连接串 / 方言 / 只读位可能变了，丢弃旧连接池，下次按需重建
    await db.dispose_source_engine(row.id)
    return row


async def delete(session: AsyncSession, source_id: str) -> None:
    """删数据源：删行后再释放连接池，避免残留的池继续持有口令。"""
    row = await session.get(DataSourceRow, source_id)
    if row is None:
        raise ValueError("数据源不存在")
    await session.delete(row)
    await session.flush()
    await db.dispose_source_engine(row.id)


async def test_connection(
    session: AsyncSession,
    *,
    dialect: str,
    url: str,
    existing_id: str | None = None,
) -> DataSourceTestResult:
    """探一次连通性；带 existing_id 且 url 留空时沿用库中已存的连接串。"""
    try:
        dialect = _validate_dialect(dialect)
        target_url = (url or "").strip()
        if not target_url and existing_id:
            row = await session.get(DataSourceRow, existing_id)
            if row is None:
                return DataSourceTestResult(ok=False, detail="数据源不存在")
            target_url = decrypt_url(row.url)
            dialect = row.dialect
        if not target_url:
            return DataSourceTestResult(ok=False, detail="请先填写连接串")
        prepared = _prepare_url(dialect, target_url)
        name = await db.test_source_connection(prepared, dialect)
        # 连上了再顺带数一下可见表，给个正反馈（计数失败不影响连通结论）
        try:
            count = await _count_tables(prepared, dialect)
        except Exception:  # noqa: BLE001
            count = -1
        return DataSourceTestResult(
            ok=True, detail=f"连接成功（{name}）", table_count=count
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都转成友好结论
        logger.info("数据源连接测试失败：%s", exc)
        return DataSourceTestResult(ok=False, detail=f"连接失败：{exc}")


async def _count_tables(url: str, dialect: str) -> int:
    """数一下可见表，给连通测试当「确实连对了库」的旁证。

    用一次性的探查引擎（不进连接池缓存），所以 finally 里必须 dispose；
    各方言只查自己的元数据表，不碰业务数据。
    """
    probe = db.new_probe_engine(url, dialect)
    try:
        async with probe.connect() as conn:
            if dialect == "sqlite":
                n = await conn.scalar(
                    text(
                        "select count(*) from sqlite_master "
                        "where type='table' and name not like 'sqlite_%'"
                    )
                )
            else:
                schema = "'public'" if dialect == "postgresql" else "database()"
                n = await conn.scalar(
                    text(
                        "select count(distinct table_name) from information_schema.columns "
                        f"where table_schema = {schema}"
                    )
                )
        return int(n or 0)
    finally:
        await probe.dispose()


__all__ = [
    "create",
    "decrypt_url",
    "delete",
    "encrypt_url",
    "list_sources",
    "mask_url",
    "selectable_sources",
    "test_connection",
    "to_out",
    "update",
]
