"""外部数据源管理：CRUD + 连接串掩码 + 连通性测试。

连接串含口令，本模块负责：
- 写入时把用户填的同步串 / 裸路径补成可用的异步连接串；
- 读出时只给掩码串（口令打码），原文永不进响应；
- 改 / 删时释放对应的连接池（``db.dispose_source_engine``）。
"""

from __future__ import annotations

import logging
import time
import uuid

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db
from ..models import DataSourceRow
from ..schemas.analytics import AnalyticsSource
from ..schemas.datasource import (
    DATA_SOURCE_DIALECTS,
    DataSourceOut,
    DataSourceTestResult,
)

logger = logging.getLogger(__name__)


def _new_id() -> str:
    return f"ds_{uuid.uuid4().hex[:12]}"


def _validate_dialect(dialect: str) -> str:
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


def mask_url(url: str) -> str:
    """把连接串里的口令打码；解析失败则原样正则兜底，绝不回明文口令。"""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - 非法串也要能安全展示
        return "***"


def to_out(row: DataSourceRow) -> DataSourceOut:
    return DataSourceOut(
        id=row.id,
        name=row.name,
        dialect=row.dialect,
        url_masked=mask_url(row.url),
        readonly=row.readonly,
        enabled=row.enabled,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _unique_name(
    session: AsyncSession, name: str, *, exclude_id: str | None = None
) -> None:
    existing = await session.scalar(select(DataSourceRow).where(DataSourceRow.name == name))
    if existing is not None and existing.id != exclude_id:
        raise ValueError(f"数据源名称已存在：{name}")


async def list_sources(session: AsyncSession) -> list[DataSourceOut]:
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
        url=prepared,
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
        row.url = _prepare_url(row.dialect, url)
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
            target_url = row.url
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
    "delete",
    "list_sources",
    "mask_url",
    "selectable_sources",
    "test_connection",
    "to_out",
    "update",
]
