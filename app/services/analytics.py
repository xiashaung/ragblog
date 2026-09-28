"""智能问数（Text-to-SQL）服务。

设计目标：让后台用户用自然语言提问，针对**选定的数据源**自动
  ① 读取该库的表结构 → ② 让大模型生成一条 **只读** SQL → ③ 强校验 + 只读事务兜底执行
  → ④ 把结果再交给大模型总结成人话结论。

数据源：``source`` 为 ``"main"`` 时分析本应用主库；否则按 ``DataSourceRow``
的 id / name 解析出对应的外部引擎（MySQL / SQLite / Postgres）。

安全红线（三重）：
1. ``guard_read_only_sql`` —— 纯 Python 校验：单语句、仅 SELECT/WITH、无禁用关键字、不碰系统表。
2. 执行时走 **独立连接 + 只读事务**（Postgres ``SET TRANSACTION READ ONLY``，MySQL 尽力设
   会话只读），并设语句超时，结束一律 ``rollback``，即便校验被绕过也写不进库。
3. 凭证列（password/token/api_key…）从 schema 上下文与结果行里一并抹掉，SELECT * 也拿不到。
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import suppress

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .. import db
from ..config import get_settings
from ..db import get_engine
from ..models import AnalyticsChatRow, DataSourceRow
from ..schemas.ai import ChatMessage, Provider, Usage
from ..schemas.analytics import (
    AnalyticsColumn,
    AnalyticsHistoryItem,
    AnalyticsHistoryResponse,
    AnalyticsQueryRequest,
    AnalyticsQueryResponse,
    AnalyticsSchemaResponse,
    AnalyticsTable,
)
from . import datasource, llm

logger = logging.getLogger(__name__)

# 语句必须以 SELECT / WITH 开头（只读）。
_STATEMENT_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

# 整词黑名单：任何可能改库 / 越权 / 卡库的关键字与危险函数。
# 因为已限制语句以 SELECT/WITH 开头且禁止分号与注释，这里主要兜住
# Postgres 的「数据修改型 CTE」（WITH x AS (DELETE ... RETURNING)）与副作用函数。
_FORBIDDEN_WORDS: frozenset[str] = frozenset(
    {
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "create",
        "truncate",
        "grant",
        "revoke",
        "copy",
        "vacuum",
        "analyze",
        "attach",
        "detach",
        "pragma",
        "merge",
        "upsert",
        "into",
        "begin",
        "commit",
        "rollback",
        "savepoint",
        "listen",
        "unlisten",
        "notify",
        "lock",
        "refresh",
        "reindex",
        "cluster",
        "discard",
        "shutdown",
        "pg_sleep",
        "pg_sleep_for",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
    }
)

# 统一产出 6 列：表名、列名、类型、可空、表注释、列注释（SQLite 无注释，用空串占位）。
# 表/列注释是模型选对表的关键依据（如 oa_project_task=项目任务表）。
_PG_SCHEMA_SQL = """
select c.table_name, c.column_name, c.data_type, c.is_nullable,
       coalesce(obj_description(pgc.oid), '') as table_comment,
       coalesce(col_description(pgc.oid, a.attnum), '') as column_comment
  from information_schema.columns c
  join pg_class pgc on pgc.relname = c.table_name
  join pg_namespace ns on ns.oid = pgc.relnamespace and ns.nspname = c.table_schema
  join pg_attribute a on a.attrelid = pgc.oid and a.attname = c.column_name and a.attnum > 0
 where c.table_schema = 'public'
 order by c.table_name, c.ordinal_position
"""

_MY_SCHEMA_SQL = """
select c.table_name, c.column_name, c.data_type, c.is_nullable,
       coalesce(t.table_comment, '') as table_comment,
       coalesce(c.column_comment, '') as column_comment
  from information_schema.columns c
  join information_schema.tables t
    on t.table_schema = c.table_schema and t.table_name = c.table_name
 where c.table_schema = database()
 order by c.table_name, c.ordinal_position
"""

_SQLITE_SCHEMA_SQL = """
select m.name as table_name, p.name as column_name, p.type as data_type,
       'YES' as is_nullable, '' as table_comment, '' as column_comment
  from sqlite_master m
  join pragma_table_info(m.name) p
 where m.type = 'table' and m.name not like 'sqlite_%'
 order by m.name, p.cid
"""

# 表太多时先让模型挑相关表，避免整库 schema 撑爆上下文导致它瞎猜字段。
_SCHEMA_AUTOCAP_TABLES = 24
# 执行报错后最多让模型自我修正 SQL 的次数（每次都重新只读校验再执行）。
_MAX_SQL_FIX_ROUNDS = 2


def guard_read_only_sql(
    raw: str, *, denied_tables: Sequence[str] = ()
) -> tuple[bool, str, str]:
    """校验并清洗出一条只读 SQL。

    返回 ``(是否通过, 清洗后的 SQL, 错误信息)``。纯函数，不碰数据库，便于单测。
    """
    if not raw or not raw.strip():
        return False, "", "空 SQL"
    sql = raw.strip()
    # 去注释（块注释、行注释）——注释里可能夹带分号或关键字
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        return False, "", "空 SQL"
    if ";" in sql:
        return False, "", "只允许执行单条语句（检测到分号）"
    if not _STATEMENT_START.match(sql):
        return False, "", "只允许 SELECT / WITH 查询（只读）"
    lowered = sql.lower()
    for word in sorted(_FORBIDDEN_WORDS):
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return False, "", f"检测到禁止的关键字 / 函数：{word.upper()}"
    for table in denied_tables:
        name = table.strip().lower()
        if name and re.search(rf"\b{re.escape(name)}\b", lowered):
            return False, "", f"该表不可访问：{table}"
    return True, sql, ""


def _secret_columns() -> set[str]:
    """凭证列名（小写集）：这些列连表结构都不给模型看。"""
    return {c.lower() for c in get_settings().analytics_secret_columns if c}


def _jsonify(value: object) -> object:
    """把库返回值收敛成 JSON 可序列化的标量。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return str(value)  # Decimal / datetime / date / UUID / pgvector ...


def _schema_sql_for(dialect: str) -> str:
    """按方言选「查元数据」的 SQL；认不了的方言一律退回 SQLite 版。"""
    if dialect == "postgresql":
        return _PG_SCHEMA_SQL
    if dialect == "mysql":
        return _MY_SCHEMA_SQL
    return _SQLITE_SCHEMA_SQL


async def _resolve_source(
    session: AsyncSession, source: str | None
) -> tuple[AsyncEngine, bool]:
    """把 ``source`` 解析成 ``(目标引擎, 是否强制只读)``。

    ``main`` → 主库引擎（恒只读）；否则查 ``DataSourceRow``（按 id，退化按 name）
    并复用 / 新建其引擎。停用或不存在的源都直接报错（路由层转友好提示）。
    """
    key = (source or "main").strip() or "main"
    if key == "main":
        return get_engine(), True
    row = await session.get(DataSourceRow, key)
    if row is None:
        row = await session.scalar(
            select(DataSourceRow).where(DataSourceRow.name == key)
        )
    if row is None:
        raise ValueError(f"数据源不存在：{source}")
    if not row.enabled:
        raise ValueError(f"数据源已停用：{row.name}")
    engine = await db.get_source_engine(row.id, datasource.decrypt_url(row.url), row.dialect)
    return engine, bool(row.readonly)


async def _collect_tables(engine: AsyncEngine) -> list[AnalyticsTable]:
    """在目标引擎上自省表结构（剔除禁用表与凭证列）。"""
    settings = get_settings()
    denied = {t.lower() for t in settings.analytics_denied_tables}
    secret = _secret_columns()
    sql = _schema_sql_for(engine.dialect.name)
    async with engine.connect() as conn:
        rows = (await conn.execute(text(sql))).all()

    grouped: OrderedDict[str, AnalyticsTable] = OrderedDict()
    for table_name, column_name, data_type, is_nullable, t_comment, c_comment in rows:
        if table_name.lower() in denied:
            continue
        if column_name.lower() in secret:
            continue
        table = grouped.get(table_name)
        if table is None:
            table = AnalyticsTable(
                name=table_name, columns=[], comment=str(t_comment or "")
            )
            grouped[table_name] = table
        table.columns.append(
            AnalyticsColumn(
                name=column_name,
                type=data_type or "",
                nullable=(str(is_nullable).upper() != "NO"),
                comment=str(c_comment or ""),
            )
        )
    return list(grouped.values())


async def fetch_schema(
    session: AsyncSession, source: str = "main"
) -> AnalyticsSchemaResponse:
    """读取指定数据源的表结构（剔除禁用表与凭证列），供前端展示与喂给模型。"""
    engine, _ = await _resolve_source(session, source)
    tables = await _collect_tables(engine)
    return AnalyticsSchemaResponse(
        dialect=engine.dialect.name,
        database=engine.url.database or "",
        source=(source or "main").strip() or "main",
        tables=tables,
    )


def _schema_for_prompt(tables: Sequence[AnalyticsTable]) -> str:
    """把表结构压成给模型看的紧凑文本，带上表/列注释（选对表的关键语义）。"""
    lines: list[str] = []
    for table in tables:
        cols = ", ".join(
            f"{c.name} {c.type}「{c.comment}」" if c.comment else f"{c.name} {c.type}"
            for c in table.columns
        )
        head = f"{table.name}（{table.comment}）" if table.comment else table.name
        lines.append(f"{head}({cols})")
    return "\n".join(lines)


def _parse_sql_json(content: str) -> tuple[str, str]:
    """从模型输出里取 {sql, brief}；容忍 ```json 包裹或裸 SQL。"""
    text_ = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text_, flags=re.DOTALL)
    if fenced:
        text_ = fenced.group(1).strip()
    try:
        data = json.loads(text_)
        if isinstance(data, dict):
            return str(data.get("sql", "")).strip(), str(data.get("brief", "")).strip()
    except (ValueError, TypeError):
        pass
    # 兜底：模型没按 JSON 返回，就把整段当 SQL 交给后续校验
    return text_, ""


async def _generate_sql(
    backend: llm.LLMBackend, question: str, schema_text: str, dialect: str
) -> tuple[str, str, Usage | None]:
    """让模型把自然语言问题译成一条只读 SQL。

    temperature=0 求稳定，``json_object`` 求可控格式；提示词里把「只能读 / 只能用
    出现过的表列名 / 结果可能很大时自行 LIMIT」写成硬要求，安全校验是另一道闸门。
    """
    system = (
        "你是资深数据分析师。依据下面给出的数据库表结构，把用户问题翻译成"
        "**一条只读 SQL**。\n"
        f"数据库方言：{dialect}。\n"
        "表结构里，表名后括号内是该表的中文说明（注释），列名后的「」是列的"
        "业务含义；先根据注释确认该用哪张表，同名相似的表（如主表与日志表/"
        "配置表）不要选错。\n"
        "要求：\n"
        "1. 只能是 SELECT 或 WITH...SELECT；严禁 INSERT/UPDATE/DELETE/DROP/ALTER/"
        "CREATE 等任何写操作，严禁多语句与结尾分号；\n"
        "2. 只能使用下列出现过的表名与列名，绝不臆造；\n"
        "3. 结果可能很大时自行加合理的 LIMIT 或做聚合；\n"
        '只返回 JSON：{"sql": "<最终 SQL>", "brief": "<一句话说明在查什么>"}。\n\n'
        f"表结构：\n{schema_text}"
    )
    content, _finish, usage, _model = await backend.chat(
        [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=question),
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    sql, brief = _parse_sql_json(content)
    return sql, brief, usage


def _tables_mentioned(sql: str, tables: Sequence[AnalyticsTable]) -> list[AnalyticsTable]:
    """挑出 SQL 文本里提到的表（用于修正时只喂相关表，减少噪声）。"""
    low = sql.lower()
    return [t for t in tables if re.search(rf"\b{re.escape(t.name.lower())}\b", low)]


async def _select_relevant_tables(
    backend: llm.LLMBackend, question: str, tables: Sequence[AnalyticsTable]
) -> list[AnalyticsTable]:
    """表很多时先让模型按问题挑相关表，缩小后再给字段级 schema，降低幻觉。"""
    names = [f"{t.name}（{t.comment}）" if t.comment else t.name for t in tables]
    system = (
        "你在为「文生 SQL」挑选需要用到的数据库表。括号内是表的中文说明，"
        "据此判断业务含义，主表与日志表/配置表/未启用的同名表不要混选。"
        "只从给定表里选，选出回答该问题**必须**用到的表（尽量精，一般 1-5 张）。\n"
        '只返回 JSON：{"tables": ["表名", ...]}，表名不含括号说明。'
    )
    user = f"问题：{question}\n\n可选表：\n" + ", ".join(names)
    try:
        content, _f, _u, _m = await backend.chat(
            [
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=user),
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        text_ = content.strip()
        fenced = re.search(r"```(?:json)?\s*(.+?)```", text_, flags=re.DOTALL)
        if fenced:
            text_ = fenced.group(1).strip()
        chosen: list[str] = []
        try:
            data = json.loads(text_)
            raw = data.get("tables") if isinstance(data, dict) else None
            if isinstance(raw, list):
                chosen = [str(x).strip().lower() for x in raw]
        except (ValueError, TypeError):
            pass
        by_name = {t.name.lower(): t for t in tables}

        def _match(x: str) -> AnalyticsTable | None:
            """容错比对：模型可能连括号说明一起返回，只取表名部分。"""
            base = re.split(r"[（(]", x.strip(), maxsplit=1)[0].strip().lower()
            return by_name.get(base)

        picked = [t for n in chosen if (t := _match(n)) is not None]
        return picked or list(tables)  # 没挑出来就退回全量
    except Exception:  # noqa: BLE001 - 选表失败不致命，退回全量
        return list(tables)


async def _repair_sql(
    backend: llm.LLMBackend,
    question: str,
    schema_text: str,
    dialect: str,
    bad_sql: str,
    error: str,
) -> tuple[str, str, Usage | None]:
    """把「失败的 SQL + 数据库报错 + 相关表字段」回喂给模型，让它改对再用。"""
    system = (
        "上一条 SQL 在数据库执行报错了。请**只依据下面给出的表和字段**修正它，"
        "不要使用列表里没有的列名或表名；若报错是字段不存在，优先怀疑选错了表，"
        "对照表注释重新选表。数据库方言："
        f"{dialect}。仍须是**单条只读** SELECT/WITH。\n"
        '只返回 JSON：{"sql": "<修正后的 SQL>", "brief": "<改了什么>"}。'
    )
    user = (
        f"原始问题：{question}\n"
        f"出错的 SQL：{bad_sql}\n"
        f"数据库报错：{error}\n\n"
        f"可用表结构：\n{schema_text}"
    )
    content, _finish, usage, _model = await backend.chat(
        [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user),
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    sql, brief = _parse_sql_json(content)
    return sql, brief, usage


def _friendly_db_error(exc: Exception) -> str:
    """把底层数据库报错翻译成可执行建议，且不外泄原始 SQL / 堆栈。"""
    msg = str(exc)
    low = msg.lower()
    if "no such column" in low or "unknown column" in low or "1054" in msg:
        return "SQL 引用了不存在的字段，自动修正后仍未成功；请把问题聚焦到某张表或换种说法。"
    if (
        "no such table" in low
        or "unknown table" in low
        or "doesn't exist" in low
        or "1146" in msg
    ):
        return "SQL 引用了不存在的表，请确认所选数据源是否包含相关数据。"
    if "syntax" in low or "1064" in msg:
        return "生成的 SQL 语法有误，自动修正后仍未成功，可换个问法再试。"
    if (
        "timeout" in low
        or "max_execution_time" in low
        or "interrupted" in low
        or "canceled" in low
    ):
        return "查询超时被中断，请缩小范围（加时间/条件过滤或减少列）。"
    return f"查询执行失败：{msg[:200]}"


async def _run_readonly(
    engine: AsyncEngine,
    sql: str,
    *,
    read_only: bool = True,
    max_rows: int,
    timeout_seconds: float,
) -> tuple[list[str], list[list[object]], bool]:
    """在独立连接的只读事务里执行，逐行拉取到 max_rows 即截断，结束一律回滚。"""
    dialect = engine.dialect.name
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            if read_only and dialect == "postgresql":
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                await conn.execute(
                    text(f"SET LOCAL statement_timeout = {int(timeout_seconds * 1000)}")
                )
            elif read_only and dialect == "mysql":
                # 尽力把会话设为只读 + 语句超时；真兜底仍是 Python 校验 + 永不提交。
                with suppress(Exception):
                    await conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
                if timeout_seconds > 0:
                    with suppress(Exception):
                        await conn.execute(
                            text(
                                "SET SESSION MAX_EXECUTION_TIME = "
                                f"{int(timeout_seconds * 1000)}"
                            )
                        )
            result = await conn.stream(text(sql))
            columns = list(result.keys())
            rows: list[list[object]] = []
            truncated = False
            async for row in result:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append([_jsonify(v) for v in row])
        finally:
            await trans.rollback()  # 只读，永不提交

    # 凭证列二次屏蔽（防 SELECT * 带出 password/token）
    secret = _secret_columns()
    keep = [i for i, col in enumerate(columns) if col.lower() not in secret]
    if len(keep) != len(columns):
        columns = [columns[i] for i in keep]
        rows = [[row[i] for i in keep] for row in rows]
    return columns, rows, truncated


def _rows_to_text(
    columns: Sequence[str], rows: Sequence[Sequence[object]], limit: int = 40
) -> str:
    """把结果集压成给模型看的管道分隔表格（只取前 ``limit`` 行、每格截 60 字）。"""
    lines = [" | ".join(str(c) for c in columns)]
    for row in rows[:limit]:
        lines.append(" | ".join("" if v is None else str(v)[:60] for v in row))
    return "\n".join(lines)


async def _summarize(
    backend: llm.LLMBackend,
    question: str,
    sql: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
) -> str:
    """把「问题 + SQL + 结果前若干行」交给模型，要一段中文结论。

    空结果不花一次调用，直接回固定文案；提示词只要求「根据给出的结果说」，
    结果里没有的信息不让编。
    """
    if not rows:
        return "查询执行成功，但没有匹配到任何数据。"
    user = (
        f"问题：{question}\n"
        f"SQL：{sql}\n"
        f"结果（共 {len(rows)} 行，下面为前 {min(len(rows), 40)} 行）：\n"
        f"{_rows_to_text(columns, rows)}"
    )
    content, _f, _u, _m = await backend.chat(
        [
            ChatMessage(
                role="system",
                content=(
                    "你是数据分析助手。只依据给出的查询结果，用简洁中文回答用户问题，"
                    "必要时点出关键数字与趋势；结果里没有的信息不要编造。"
                ),
            ),
            ChatMessage(role="user", content=user),
        ],
        temperature=0.2,
    )
    return content


async def query(
    session: AsyncSession, payload: AnalyticsQueryRequest
) -> AnalyticsQueryResponse:
    """一次问数：解析数据源 → 生成 SQL → 校验 →（可选）执行 + 总结。

    任何一步失败都以 ``error`` 友好反馈（仍返回 200），不抛 500。
    """
    settings = get_settings()
    source = (payload.source or "main").strip() or "main"
    backend = llm.get_llm(payload.provider)
    provider: Provider = backend.provider

    try:
        engine, read_only = await _resolve_source(session, source)
    except Exception as exc:  # noqa: BLE001 - 源不可用也不应 500
        logger.warning("解析数据源失败：%s", exc)
        return AnalyticsQueryResponse(
            question=payload.question,
            provider=provider,
            source=source,
            error=f"数据源不可用：{exc}",
        )

    try:
        tables = await _collect_tables(engine)
    except Exception as exc:  # noqa: BLE001 - 读结构失败也不应 500
        logger.warning("读取表结构失败：%s", exc)
        return AnalyticsQueryResponse(
            question=payload.question,
            provider=provider,
            source=source,
            error=f"读取数据库表结构失败：{exc}",
        )

    dialect = engine.dialect.name
    # 表太多时先挑相关表，避免整库 schema 撑爆上下文导致模型瞎猜字段
    schema_tables = (
        await _select_relevant_tables(backend, payload.question, tables)
        if len(tables) > _SCHEMA_AUTOCAP_TABLES
        else tables
    )
    try:
        raw_sql, brief, usage = await _generate_sql(
            backend, payload.question, _schema_for_prompt(schema_tables), dialect
        )
    except Exception as exc:  # noqa: BLE001 - 上游模型异常转成友好 error
        logger.warning("生成 SQL 失败：%s", exc)
        return AnalyticsQueryResponse(
            question=payload.question,
            provider=provider,
            source=source,
            dialect=dialect,
            error=f"生成 SQL 失败：{exc}",
        )

    ok, sql, err = guard_read_only_sql(
        raw_sql, denied_tables=settings.analytics_denied_tables
    )
    if not ok:
        return AnalyticsQueryResponse(
            question=payload.question,
            provider=provider,
            source=source,
            dialect=dialect,
            sql=raw_sql[:2000],
            brief=brief,
            error=f"生成的 SQL 未通过只读校验：{err}",
            usage=usage,
        )

    if payload.dry_run:
        return AnalyticsQueryResponse(
            question=payload.question,
            provider=provider,
            source=source,
            dialect=dialect,
            sql=sql,
            brief=brief,
            usage=usage,
        )

    # 执行；如果字段/表写错而失败，就把报错回喂给模型修正后重试（每轮都重新只读校验）
    fix_round = 0
    while True:
        try:
            columns, rows, truncated = await _run_readonly(
                engine,
                sql,
                read_only=read_only,
                max_rows=settings.analytics_max_rows,
                timeout_seconds=settings.analytics_timeout_seconds,
            )
            break
        except Exception as exc:  # noqa: BLE001 - 交给自愈重试 / 转友好提示
            logger.info("问数 SQL 执行失败（第 %s 轮）：%s", fix_round, exc)
            if fix_round >= _MAX_SQL_FIX_ROUNDS:
                return AnalyticsQueryResponse(
                    question=payload.question,
                    provider=provider,
                    source=source,
                    dialect=dialect,
                    sql=sql,
                    brief=brief,
                    error=_friendly_db_error(exc),
                    usage=usage,
                )
            fix_round += 1
            focus = _tables_mentioned(sql, tables) or schema_tables
            try:
                raw_sql, brief, usage2 = await _repair_sql(
                    backend,
                    payload.question,
                    _schema_for_prompt(focus),
                    dialect,
                    sql,
                    str(exc),
                )
            except Exception:  # noqa: BLE001 - 修正调用本身失败则放弃
                return AnalyticsQueryResponse(
                    question=payload.question,
                    provider=provider,
                    source=source,
                    dialect=dialect,
                    sql=sql,
                    brief=brief,
                    error=_friendly_db_error(exc),
                    usage=usage,
                )
            ok2, sql2, err2 = guard_read_only_sql(
                raw_sql, denied_tables=settings.analytics_denied_tables
            )
            if not ok2:
                return AnalyticsQueryResponse(
                    question=payload.question,
                    provider=provider,
                    source=source,
                    dialect=dialect,
                    sql=raw_sql[:2000],
                    brief=brief,
                    error=f"修正后的 SQL 未通过只读校验：{err2}",
                    usage=usage,
                )
            sql = sql2
            usage = usage2 or usage

    conclusion = await _summarize(backend, payload.question, sql, columns, rows)
    return AnalyticsQueryResponse(
        question=payload.question,
        provider=provider,
        source=source,
        dialect=dialect,
        sql=sql,
        brief=brief,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        conclusion=conclusion,
        usage=usage,
    )


# --------------------------------------------------------------------------- #
# 问答历史（对话式问数的持久化：存 / 查 / 删 / 清空，仅本人可见）
# --------------------------------------------------------------------------- #


async def save_chat(
    session: AsyncSession, admin_id: str, resp: AnalyticsQueryResponse
) -> AnalyticsChatRow:
    """把一次问答落库（失败的结果也存，便于会话流回放与删除）。"""
    row = AnalyticsChatRow(
        admin_id=admin_id,
        source=resp.source,
        question=resp.question[:2000],
        result_json=resp.model_dump_json(),
    )
    session.add(row)
    await session.commit()
    resp.chat_id = row.id
    resp.created_at = row.created_at
    return row


async def list_history(
    session: AsyncSession, admin_id: str, *, limit: int = 100
) -> AnalyticsHistoryResponse:
    """按时间正序返回本人最近 ``limit`` 条问答，供对话流回放。"""
    rows = (
        await session.scalars(
            select(AnalyticsChatRow)
            .where(AnalyticsChatRow.admin_id == admin_id)
            .order_by(AnalyticsChatRow.created_at.desc())
            .limit(limit)
        )
    ).all()
    items: list[AnalyticsHistoryItem] = []
    for row in reversed(list(rows)):
        try:
            result = AnalyticsQueryResponse.model_validate_json(row.result_json)
        except Exception:  # noqa: BLE001 - 脏数据不该击穿整个历史
            logger.warning("跳过无法解析的历史消息：%s", row.id)
            continue
        result.chat_id = row.id
        result.created_at = row.created_at
        items.append(
            AnalyticsHistoryItem(
                id=row.id,
                question=row.question,
                source=row.source,
                created_at=row.created_at,
                result=result,
            )
        )
    return AnalyticsHistoryResponse(messages=items)


async def delete_chat(session: AsyncSession, admin_id: str, chat_id: str) -> None:
    """删除单条问答（只能删自己的；不存在视同已删，不报错）。"""
    row = await session.get(AnalyticsChatRow, chat_id)
    if row is not None and row.admin_id == admin_id:
        await session.delete(row)
        await session.commit()


async def clear_history(session: AsyncSession, admin_id: str) -> int:
    """清空本人全部历史，返回删除条数。"""
    rows = (
        await session.scalars(
            select(AnalyticsChatRow).where(AnalyticsChatRow.admin_id == admin_id)
        )
    ).all()
    for row in rows:
        await session.delete(row)
    await session.commit()
    return len(rows)


__all__ = [
    "clear_history",
    "delete_chat",
    "fetch_schema",
    "guard_read_only_sql",
    "list_history",
    "query",
    "save_chat",
]
