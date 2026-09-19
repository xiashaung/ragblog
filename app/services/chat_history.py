"""AI 对话会话持久化（``chat_sessions`` / ``chat_messages``）。

约定：

- 一切按 ``admin_id`` 隔离。越权访问统一返回 ``None``，由路由层报 404 —— 不区分
  「会话不存在」和「不是你的会话」，避免被人拿来探测他人的会话 id。
- 消息由前端在**一次问答完成后**整轮追加（用户消息 + 助手消息）。不在流式接口里
  写库：``_stream_chat`` 的生成器生命周期长于请求级 DB session，往里塞 commit 不可靠，
  而且中途断开会把半截回答存成正式消息。
- 图片是 data URL，体积可达数 MB。单条消息图片总长超过 ``MAX_IMAGES_BYTES`` 时
  只丢图保留正文，并在 ``meta_json`` 记 ``images_dropped``，让回放方能明说
  「图片未持久化」而不是默默缺图。
- ``seq`` 在会话内递增，用于稳定排序（``created_at`` 是秒级 Float，同一轮的两条会撞）。
- 联网回答只存引用链接（``meta_json.web_sources``），不存工具回喂给模型的搜索全文：
  那是当时那一轮的中间产物，存下来既占空间也无法重现（搜索引擎下次给的结果不一样）。
- 思考过程存 ``meta_json.thinking``：它和引用链接一样是「这一答怎么来的」的凭据，
  回放时要能展开看；体积是文字量级，不像图片需要丢弃阈值。
  只存不回喂：拼上下文时只取 ``content``，把推理塞回去会让 prompt 成倍膨胀。
"""

from __future__ import annotations

import json
import logging
import time

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChatMessageRow, ChatSessionRow
from ..schemas.ai import (
    ChatAppendResponse,
    ChatMessageIn,
    ChatMessageListResponse,
    ChatMessageOut,
    ChatSessionListResponse,
    ChatSessionOut,
    WebSource,
)

logger = logging.getLogger(__name__)

#: 新建会话的占位标题；首条提问入库后被自动替换（用户懒得改名也有意义）
DEFAULT_TITLE = "新对话"

#: 单条消息可持久化的图片 data URL 总字节数，超出只丢图
MAX_IMAGES_BYTES = 2_000_000

#: 自动标题取问题前多少字
TITLE_CHARS = 24


def _derive_title(text: str) -> str:
    """用首条提问当前 24 字作标题：压掉换行与多余空白，避免列表里出现整段引文。"""
    flat = " ".join(text.split())
    if not flat:
        return DEFAULT_TITLE
    return flat[:TITLE_CHARS] + ("…" if len(flat) > TITLE_CHARS else "")


def _session_out(row: ChatSessionRow, message_count: int = 0) -> ChatSessionOut:
    return ChatSessionOut(
        id=row.id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        message_count=message_count,
    )


def _loads(raw: str | None, fallback):
    """脏 JSON 不该击穿整个历史（与问数历史同一策略）：解析失败退回默认值。"""
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _message_out(row: ChatMessageRow) -> ChatMessageOut:
    meta = _loads(row.meta_json, {})
    usage = meta.get("usage") if isinstance(meta, dict) else None
    raw_sources = meta.get("web_sources") if isinstance(meta, dict) else None
    thinking = meta.get("thinking") if isinstance(meta, dict) else None
    sources: list[WebSource] = []
    for item in raw_sources or []:
        # 脏数据不能击穿整个历史：单条解析失败就跳过它
        try:
            sources.append(WebSource.model_validate(item))
        except ValidationError:
            continue
    return ChatMessageOut(
        id=row.id,
        seq=row.seq,
        role=row.role,
        content=row.content,
        images=[x for x in _loads(row.images_json, []) if isinstance(x, str)],
        usage=usage,
        images_dropped=int(meta.get("images_dropped", 0)) if isinstance(meta, dict) else 0,
        web_sources=sources,
        thinking=thinking if isinstance(thinking, str) else "",
        created_at=row.created_at,
    )


async def _owned(
    session: AsyncSession, admin_id: str, chat_id: str
) -> ChatSessionRow | None:
    """取属于本人的会话；不是自己的与不存在同样返回 ``None``。"""
    row = await session.get(ChatSessionRow, chat_id)
    if row is None or row.admin_id != admin_id:
        return None
    return row


async def list_sessions(
    session: AsyncSession, admin_id: str, *, limit: int = 50
) -> ChatSessionListResponse:
    """本人会话列表：最近活跃的在前，附带各自消息条数（一次 group by 统计）。"""
    rows = (
        await session.scalars(
            select(ChatSessionRow)
            .where(ChatSessionRow.admin_id == admin_id)
            .order_by(ChatSessionRow.updated_at.desc())
            .limit(max(1, min(limit, 200)))
        )
    ).all()
    ids = [r.id for r in rows]
    counts: dict[str, int] = {}
    if ids:
        counts = {
            sid: n
            for sid, n in (
                await session.execute(
                    select(ChatMessageRow.session_id, func.count())
                    .where(ChatMessageRow.session_id.in_(ids))
                    .group_by(ChatMessageRow.session_id)
                )
            ).all()
        }
    return ChatSessionListResponse(
        sessions=[_session_out(r, counts.get(r.id, 0)) for r in rows]
    )


async def create_session(
    session: AsyncSession, admin_id: str, title: str = ""
) -> ChatSessionOut:
    """新建会话。标题留空用占位名，等首条提问再自动改名。"""
    now = time.time()
    row = ChatSessionRow(
        admin_id=admin_id,
        title=(title.strip()[:200] or DEFAULT_TITLE),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return _session_out(row)


async def rename_session(
    session: AsyncSession, admin_id: str, chat_id: str, title: str
) -> ChatSessionOut | None:
    """改名（仅本人的会话）；返回 ``None`` 表示会话不存在或不属于本人。"""
    row = await _owned(session, admin_id, chat_id)
    if row is None:
        return None
    row.title = title.strip()[:200] or DEFAULT_TITLE
    # 改名不算活跃活动，不动 updated_at，免得列表顺序被无意义地顶到最前
    await session.commit()
    return _session_out(row)


async def delete_session(
    session: AsyncSession, admin_id: str, chat_id: str
) -> bool:
    """删除会话及其全部消息。

    先按 ``session_id`` 批量删子记录，不依赖外键级联（SQLite 默认不开 FK，
    Postgres 又需要自增级联声明，两处行为不一致）。
    """
    row = await _owned(session, admin_id, chat_id)
    if row is None:
        return False
    await session.execute(
        delete(ChatMessageRow).where(ChatMessageRow.session_id == chat_id)
    )
    await session.delete(row)
    await session.commit()
    return True


async def get_messages(
    session: AsyncSession, admin_id: str, chat_id: str
) -> ChatMessageListResponse | None:
    """会话内的消息，按 ``seq`` 正序（前端对话流直接顺序渲染）。"""
    if await _owned(session, admin_id, chat_id) is None:
        return None
    rows = (
        await session.scalars(
            select(ChatMessageRow)
            .where(ChatMessageRow.session_id == chat_id)
            .order_by(ChatMessageRow.seq)
        )
    ).all()
    return ChatMessageListResponse(messages=[_message_out(r) for r in rows])


async def append_messages(
    session: AsyncSession, admin_id: str, chat_id: str, items: list[ChatMessageIn]
) -> ChatAppendResponse | None:
    """把一轮问答追加进会话，并刷新会话的活跃时间/自动标题。

    返回 ``None`` 表示会话不存在或不属于本人（前端此时应重建会话再发）。
    """
    row = await _owned(session, admin_id, chat_id)
    if row is None:
        return None

    last_seq = (
        await session.scalar(
            select(func.max(ChatMessageRow.seq)).where(
                ChatMessageRow.session_id == chat_id
            )
        )
        or 0
    )
    first_user_text = ""
    stored: list[ChatMessageRow] = []
    for item in items:
        images_json = json.dumps(item.images, ensure_ascii=False)
        dropped = 0
        if item.images and len(images_json.encode("utf-8")) > MAX_IMAGES_BYTES:
            # 图太大就只留正文：宁可丢附件，也不能让一次贴图上把库撑爆
            logger.warning(
                "会话 %s 的图片过大（%s 字节），仅持久化正文", chat_id, len(images_json)
            )
            dropped = len(item.images)
            images_json = "[]"
        meta: dict[str, object] = {}
        if item.usage is not None:
            meta["usage"] = item.usage.model_dump()
        if item.web_sources:
            meta["web_sources"] = [s.model_dump() for s in item.web_sources]
        if item.thinking.strip():
            # 只存本轮的最终思考（路由层已按轮取最后一份），历史里的每一答对自己的那次提问
            meta["thinking"] = item.thinking.strip()
        if dropped:
            meta["images_dropped"] = dropped
        if not first_user_text and item.role == "user":
            first_user_text = item.content
        last_seq += 1
        msg = ChatMessageRow(
            session_id=chat_id,
            role=item.role,
            content=item.content,
            images_json=images_json,
            meta_json=json.dumps(meta, ensure_ascii=False),
            seq=last_seq,
            created_at=time.time(),
        )
        session.add(msg)
        stored.append(msg)

    if row.title == DEFAULT_TITLE and first_user_text:
        row.title = _derive_title(first_user_text)
    row.updated_at = time.time()
    await session.commit()

    return ChatAppendResponse(
        session=_session_out(row, last_seq),
        messages=[_message_out(m) for m in stored],
    )


__all__ = [
    "DEFAULT_TITLE",
    "append_messages",
    "create_session",
    "delete_session",
    "get_messages",
    "list_sessions",
    "rename_session",
]
