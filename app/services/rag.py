"""检索增强生成（RAG）：切块、入库、检索、给答案。

向量库就住在应用自己的数据库里：Postgres 上 ``vector`` 是真 pgvector 列
（``vector(<APP_RAG_VECTOR_DIM>)``），余弦距离与 top-k 由库内算、走 HNSW 索引；
SQLite 上同一列退化成 JSON、打分回到 Python 算，测试与轻量开发无需装扩展。

一次录入的流程（所有入口共用）：
    取文本（文件/裸文本/问答对/URL）→ :func:`upsert_document`
    → :func:`chunk_document` 切块（Markdown 按标题切节）→ 批量 embedding → 写 ``knowledge_chunks``

检索则是：向量召回（Postgres 上可叠 pg_trgm 词法召回 → RRF 融合）
→（可选）精排 → 拼上下文交给对话模型。

embedding 可用任意已配置的 provider（见 ``llm.get_llm``）；文档行会记下录入时用的
provider 名，检索时可据此限定「只用某个 provider 录入的文档」。不同 provider 的向量
不可比，所以换模型必须重建索引（:func:`reindex_knowledge`）。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from collections.abc import Sequence

from sqlalchemy import delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import media_gc
from ..config import get_settings
from ..models import KnowledgeChunk, KnowledgeDoc
from ..schemas.ai import (
    ChatMessage,
    Citation,
    EmbeddingRequest,
    KnowledgeAnswerRequest,
    KnowledgeAnswerResponse,
    KnowledgeDocUpdateRequest,
    KnowledgeFetchRequest,
    KnowledgeFetchResponse,
    KnowledgeIngestRequest,
    KnowledgeIngestResponse,
    KnowledgePairsIngestRequest,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    Provider,
)
from . import llm, reranker, web_page
from .markdown_text import normalize_content, stored_format, strip_markdown

DEFAULT_CHUNK_SIZE = 500
# 拼标题前缀后至少给正文留这么多字符，否则前缀太吃掉预算，退回不加前缀的普通切分。
MIN_PREFIX_BUDGET = 64
PREVIEW_CHARS = 120
SNIPPET_CHARS = 500
# FAQ 标记：把问句放在每块开头，让 embedding 先对齐问题（用户提问的措辞更像问句）。
FAQ_ASK_PREFIX = "问："
FAQ_ANSWER_PREFIX = "答："

logger = logging.getLogger(__name__)


def _chunk_size_of(
    requested: int | None, meta: dict | None = None, fallback: int = DEFAULT_CHUNK_SIZE
) -> int:
    """定分片长度：请求里显式传的 > 文档存过的 > 默认值。"""
    if requested:
        return requested
    stored = (meta or {}).get("chunk_size")
    return stored if isinstance(stored, int) and stored > 0 else fallback


# 句末标点：中英混合都认；前引号/括号跟随句子走，后引号并入句尾，
# 第二个分支兜住没有任何标点的长串（整段交给定长硬切）。
_SENTENCE_RE = re.compile(r"[^。！？；.!?]+[。！？；.!?]+[""'）)】」』]*|[^。！？；.!?]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
# ATX 标题与代码围栏（围栏里的 ``#`` 不是标题）。
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _join_sentences(current: str, sent: str) -> str:
    """把句子接回片段：中文句末直接拼，英文之间补一个空格。"""
    if not current:
        return sent
    if current[-1] in "。！？；" or _CJK_RE.match(sent[0]):
        return current + sent
    return f"{current} {sent}"


def _split_long_paragraph(para: str, width: int, overlap: int) -> list[str]:
    """把超长段落按句子边界打包成不超过 ``width`` 的 piece。

    先按中英句末标点切句再 packing；只有单个句子仍超宽（整段没标点，
    比如一长串数字/日志）才退回原来的定长硬切，保证一定有产出。
    """
    pieces: list[str] = []
    current = ""
    for sent in (s.strip() for s in _SENTENCE_RE.findall(para)):
        if not sent:
            continue
        if len(sent) > width:
            if current:
                pieces.append(current)
                current = ""
            step = max(1, width - overlap)
            pieces.extend(
                sent[start : start + width] for start in range(0, len(sent), step)
            )
            continue
        if current and len(current) + len(sent) > width:
            pieces.append(current)
            current = sent
        else:
            current = _join_sentences(current, sent)
    if current:
        pieces.append(current)
    return pieces


def chunk_text(text: str, chunk_size: int = 500, overlap: int | None = None) -> list[str]:
    """按段落/句子边界切成带重叠的分片。

    段落 packing 到 ``chunk_size`` 为止；单段超长时先按句末标点切句再打包
    （见 :func:`_split_long_paragraph`），完全没有标点的长串才定长硬切，
    所以 CSV、日志、无空格中文也都能得到不超过定长的分片。
    重叠窗口默认按 ``chunk_size`` 的 1/10 取（太小则语义断裂，太大则稀释），
    每个返回分片都不超过 ``chunk_size`` 字符（含开头的 overlap 尾巴）。
    """
    if overlap is None:
        overlap = max(16, chunk_size // 10)
    overlap = max(0, min(overlap, chunk_size // 2))
    # 硬切宽度：留出位置给下一次 flush 时拼在前面的 overlap 尾巴，拼完仍不超 chunk_size。
    width = max(1, chunk_size - overlap - 1)

    pieces: list[str] = []
    for para in (p.strip() for p in text.split("\n")):
        if not para:
            continue
        if len(para) <= width:
            pieces.append(para)
            continue
        pieces.extend(_split_long_paragraph(para, width, overlap))

    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        if buffer and len(buffer) + len(piece) + 1 > chunk_size:
            chunks.append(buffer)
            # 保留尾巴作为下一块的重叠部分
            buffer = f"{buffer[-overlap:]} {piece}".strip() if overlap else piece
        else:
            buffer = f"{buffer}\n{piece}".strip() if buffer else piece
    if buffer:
        chunks.append(buffer)
    return chunks or [text[:chunk_size]]


def split_markdown_sections(text: str) -> list[tuple[list[str], str]]:
    """按 ATX 标题把 Markdown 切成节，返 ``(标题路径, 该节正文)`` 列表。

    标题用栈维护层级：``###`` 前面有 ``##`` 时路径是两层而不是拼在一起。
    代码围栏（``` / ~~~）里的 ``#`` 不算标题；标题行本身不进正文，
    由调用方拼回前缀。首个标题之前的正文单独成一节（路径为空）。
    """
    sections: list[tuple[list[str], str]] = []
    stack: list[tuple[int, str]] = []  # (层级, 标题文本)
    body_lines: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(body_lines).strip()
        if body:
            sections.append(([t for _, t in stack], body))
        body_lines.clear()

    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            body_lines.append(line)
            continue
        match = None if in_fence else _HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            # 弹出层级更深的标题，再把本级接上：路径始终是「祖先链 + 本级」。
            stack = [s for s in stack if s[0] < level] + [(level, match.group(2).strip())]
        else:
            body_lines.append(line)
    flush()
    return sections


def chunk_document(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    *,
    fmt: str = "text",
    title: str = "",
) -> list[str]:
    """结构感知的分片入口（录入与重建索引都走这里）。

    Markdown 先按标题切节，每节单独走 :func:`chunk_text`，并在每片开头
    拼上「文档标题 > 节 > 小节」的路径前缀——分片被单独 embedding 时不再
    丢失「这段在讲什么」的上下文。非 Markdown、或全文找不到任何标题时，
    退化为纯段落打包（FAQ 那条路径依赖「问：」领头，不另加前缀）。
    """
    if fmt != "markdown":
        return chunk_text(text, chunk_size=chunk_size)
    sections = split_markdown_sections(text)
    if not any(path for path, _ in sections):
        return chunk_text(text, chunk_size=chunk_size)

    chunks: list[str] = []
    for path, body in sections:
        # 正文顶格用「# 同文档标题」开头很常见，去重避免前缀里写两遍。
        parts = path if not title or (path and path[0] == title) else [title] + path
        prefix = " > ".join(parts)
        # 前缀占的字符从预算里扣掉，保证拼完仍不超过 chunk_size；
        # 预算剩得太少就不如不加前缀，否则正文被切得稀碎。
        budget = chunk_size - (len(prefix) + 1 if prefix else 0)
        if prefix and budget >= MIN_PREFIX_BUDGET:
            pieces = chunk_text(body, chunk_size=budget)
            chunks.extend(f"{prefix}\n{p}" for p in pieces)
        else:
            chunks.extend(chunk_text(body, chunk_size=chunk_size))
    return chunks


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度：只给 SQLite 退化路径用（Postgres 在库内算）。

    维度不一致或空向量一律返 0 分而不是抛错：一整库旧维度的向量里筛一两条
    不应让整次检索失败，0 分自然会被排到最后。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _resolved_provider(name: Provider | str | None) -> Provider:
    """本次请求 ``get_llm`` 会实际用哪个 provider（返枚举）。

    读的是后端实例上的 ``provider`` 而不是入参，这样响应里回写的名字
    与真正调用的一致（没传时会看到默认 provider，而不是 None）。
    """
    return llm.get_llm(name, feature="embedding").provider


def _default_provider() -> str:
    """前端不传 ``provider`` 时现在用的是哪个（列表页用来标「待重建」）。

    向量嵌入用哪个 provider 由「系统配置」的 embedding_provider 决定（没配回落 .env）。
    """
    return llm.default_provider_for("embedding")


def _vector_dim() -> int:
    """列维度：存进去的每个向量必须正好这么长。"""
    return get_settings().rag_vector_dim


def _uses_pgvector(session: AsyncSession) -> bool:
    """是否跑在真 pgvector 列上（等价于「方言是 Postgres」）。"""
    return session.get_bind().dialect.name == "postgresql"


def _dim_error(who: str, got: int, wanted: int) -> ValueError:
    """维度不符的唯一解释出处（全文中只写这一处，避免各入口说得不一致）。"""
    return ValueError(
        f"{who}的向量是 {got} 维，与知识库列维度 {wanted} 不一致。"
        "若是换了 embedding 模型：改 APP_RAG_VECTOR_DIM 并重跑 alembic upgrade head，"
        "再对文档重建索引"
    )


def _doc_kind(doc: KnowledgeDoc) -> str:
    """文档来源：file / text / faq / url（存在 meta 里，没记则推算）。"""
    kind = (doc.meta or {}).get("kind")
    if isinstance(kind, str) and kind:
        return kind
    return "file" if (doc.meta or {}).get("source_file") else "text"


def _preview_text(meta: dict, source: str) -> str:
    """列表摘要：Markdown 文档先剥掉语法符号再截断，摘出来的是文字不是 ``#`` / ``|``。"""
    head = source[: PREVIEW_CHARS * 3]
    if meta.get("format") == "markdown":
        head = strip_markdown(head)
    return head[:PREVIEW_CHARS].replace("\n", " ").strip()


def _doc_format(meta: dict) -> str:
    """正文格式回显：只认 text / markdown（html 在入口处已经转掉了），没记就算纯文本。"""
    stored = meta.get("format")
    return stored if stored in ("text", "markdown") else "text"


def _doc_item(doc: KnowledgeDoc, chunks: int, chars: int, first_text: str = "") -> dict:
    """管理列表用的文档形状（不带分片正文，避免列表接口拽出全文）。"""
    default = _default_provider()
    content = doc.content or ""
    meta = doc.meta or {}
    # 早期行没有 ``content`` 列（后来才补），回退到首个分片，列表至少有东西可预览。
    source = content or first_text
    return {
        "knowledge_id": doc.id,
        "title": doc.title,
        "provider": doc.provider,
        "chunks": chunks,
        "chars": len(content) or chars,
        "preview": _preview_text(meta, source),
        "has_content": bool(content),
        "source_file": meta.get("source_file"),
        "kind": _doc_kind(doc),
        "format": _doc_format(meta),
        "source_url": meta.get("source_url"),
        "metadata": doc.meta,
        "ingested_at": doc.ingested_at,
        "updated_at": doc.updated_at or doc.ingested_at,
        "stale": doc.provider != default,
    }


async def _first_snippets(session: AsyncSession, doc_ids: list[str]) -> dict[str, str]:
    """每篇文档的首个分片正文（只取每篇一行，不用把全文读出来）。"""
    if not doc_ids:
        return {}
    heads = (
        await session.execute(
            select(func.min(KnowledgeChunk.id))
            .where(KnowledgeChunk.knowledge_id.in_(doc_ids))
            .group_by(KnowledgeChunk.knowledge_id)
        )
    ).scalars().all()
    if not heads:
        return {}
    rows = await session.execute(
        select(KnowledgeChunk.knowledge_id, KnowledgeChunk.text).where(
            KnowledgeChunk.id.in_(heads)
        )
    )
    return {doc_id: text for doc_id, text in rows.all()}


async def _chunk_stats(session: AsyncSession, doc_ids: list[str]) -> dict[str, tuple[int, int]]:
    """每篇文档的分片数与总字数，不拉正文（统计类列表页的便宜算法）。"""
    if not doc_ids:
        return {}
    rows = await session.execute(
        select(
            KnowledgeChunk.knowledge_id,
            func.count(),
            func.coalesce(func.sum(func.length(KnowledgeChunk.text)), 0),
        )
        .where(KnowledgeChunk.knowledge_id.in_(doc_ids))
        .group_by(KnowledgeChunk.knowledge_id)
    )
    return {doc_id: (count, size) for doc_id, count, size in rows.all()}


async def replace_vectors(
    session: AsyncSession, doc: KnowledgeDoc, content: str, *, backend: llm.LLMBackend
) -> int:
    """删掉文档现有分片并重建（含重新算向量），返新分片数。

    重建索引也走这里：不同 provider/模型的 embedding 不可比，换了嵌入模型
    就只能重写向量，没有「兼容共存」这条路。返值即分片数，给录入接口回体用。
    """
    meta = doc.meta or {}
    chunk_size = _chunk_size_of(None, meta)
    chunks = chunk_document(
        content,
        chunk_size=chunk_size,
        fmt=_doc_format(meta),
        title=doc.title,
    )
    emb_resp = await backend.embeddings(
        EmbeddingRequest(provider=backend.provider, input=chunks)
    )
    # 先算出新向量再删旧分片：维度不符（或 embedding 失败）时，文档不会
    # 被削成一个没有向量的空壳。定长 vector 列装不下其他维度，这里提前报错。
    dim = _vector_dim()
    for item in emb_resp.items:
        if len(item.embedding) != dim:
            raise _dim_error(backend.provider.value, len(item.embedding), dim)
    await session.execute(
        delete(KnowledgeChunk).where(KnowledgeChunk.knowledge_id == doc.id)
    )
    for i, (text, item) in enumerate(zip(chunks, emb_resp.items, strict=False)):
        session.add(
            KnowledgeChunk(
                id=f"{doc.id}-c{i:03d}",
                knowledge_id=doc.id,
                source=doc.title,
                text=text,
                vector=item.embedding,
            )
        )
    doc.provider = backend.provider.value
    doc.updated_at = time.time()
    await session.flush()
    return len(chunks)


def _doc_id_for(title: str) -> str:
    """标题派生出固定 ID：同标题 = 同 ID，所以重复录入是「替换」而不是堆一份新的。"""
    return f"kb-{hashlib.md5(title.encode()).hexdigest()[:12]}"


async def upsert_document(
    session: AsyncSession,
    *,
    title: str,
    content: str,
    provider: Provider | str | None = None,
    chunk_size: int | None = None,
    metadata: dict | None = None,
    fmt: str = "text",
) -> KnowledgeIngestResponse:
    """入库（或整篇替换）一份文档并重建它的分片向量。

    所有录入入口——文件上传、裸文本、问答对、URL——最后都汇到这里，
    否则四路的 ID 规则、元数据写法、向量重建时机很容易各自漂移。
    注：同标题先整体 ``delete_knowledge`` 再重建，所以旧 provider 不会残留。

    ``fmt`` 是正文的录入格式：``html`` 先转成 Markdown 再切块，库里只存一种格式；
    转完实际是什么记在 ``meta["format"]``（只有 text / markdown 两个值）。
    """
    body = normalize_content(content, fmt)
    if not body:
        raise ValueError(
            "正文归一化后为空（HTML 里可能只有样式标签），请检查内容或改用 Markdown 录入"
        )

    backend = llm.get_llm(provider, feature="embedding")
    doc_id = _doc_id_for(title)

    existing = await session.get(KnowledgeDoc, doc_id)
    if existing is not None:
        await delete_knowledge(session, existing.id)

    meta = dict(metadata or {})
    if chunk_size:
        meta["chunk_size"] = chunk_size
    meta.setdefault("format", stored_format(fmt))
    doc = KnowledgeDoc(
        id=doc_id,
        title=title,
        provider=backend.provider.value,
        content=body,
        meta=meta or None,
    )
    session.add(doc)
    await session.flush()

    chunks = await replace_vectors(session, doc, body, backend=backend)
    return KnowledgeIngestResponse(
        knowledge_id=doc.id,
        provider=backend.provider,
        chunks=chunks,
        chars=len(body),
    )


async def ingest(
    session: AsyncSession, payload: KnowledgeIngestRequest
) -> KnowledgeIngestResponse:
    """裸文本录入：只负责拆请求字段，真活在 :func:`upsert_document`。"""
    return await upsert_document(
        session,
        title=payload.title,
        content=payload.content,
        provider=payload.provider,
        chunk_size=payload.chunk_size,
        metadata=payload.metadata,
        fmt=payload.format,
    )


def pairs_to_text(pairs: list[tuple[str, str]]) -> str:
    """把问答对排成「问：某句 / 答：某句」的块，块之间空一行。

    问句领每块的开头，这样一个分片的 embedding 主要由用户真正会输入的
    措辞决定（答案往往很长，会把手头的关键词稀释掉）。
    """
    return "\n\n".join(
        f"{FAQ_ASK_PREFIX}{question}\n{FAQ_ANSWER_PREFIX}{answer}"
        for question, answer in pairs
    )


async def ingest_pairs(
    session: AsyncSession, payload: KnowledgePairsIngestRequest
) -> KnowledgeIngestResponse:
    """一批 FAQ 存成一份文档（而不是每条一篇，否则列表页会被刷到花）。"""
    pairs = [
        (pair.question.strip(), pair.answer.strip())
        for pair in payload.pairs
        if pair.question.strip() and pair.answer.strip()
    ]
    if not pairs:
        raise ValueError("没有有效的问答对：问题与答案都不能为空")

    meta = dict(payload.metadata or {})
    meta.update({"kind": "faq", "pairs": len(pairs), "dropped": len(payload.pairs) - len(pairs)})
    return await upsert_document(
        session,
        title=payload.title,
        content=pairs_to_text(pairs),
        provider=payload.provider,
        chunk_size=payload.chunk_size,
        metadata=meta,
    )


async def ingest_from_url(
    session: AsyncSession, payload: KnowledgeFetchRequest
) -> KnowledgeFetchResponse:
    """抓网页，然后按页面标题入库成一份文档。"""
    page = await web_page.fetch_page(url=payload.url, max_chars=payload.max_chars)
    title = (payload.title or page.title or page.final_url).strip()[:200]

    result = await upsert_document(
        session,
        title=title,
        content=page.text,
        provider=payload.provider,
        chunk_size=payload.chunk_size,
        metadata={
            "kind": "url",
            "source_url": payload.url,
            "final_url": page.final_url,
            "page_title": page.title,
            "truncated": page.truncated,
        },
    )
    return KnowledgeFetchResponse(
        **result.model_dump(),
        title=title,
        url=payload.url,
        final_url=page.final_url,
        page_chars=len(page.text),
        truncated=page.truncated,
    )


async def list_knowledge(
    session: AsyncSession,
    *,
    keyword: str | None = None,
    provider: str | None = None,
    only_stale: bool = False,
    offset: int = 0,
    limit: int = 20,
) -> dict:
    """管理页用的分页文档列表（带总数与每页元信息）。"""
    default = _default_provider()
    stmt = select(KnowledgeDoc)
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(KnowledgeDoc.title.ilike(like), KnowledgeDoc.content.ilike(like))
        )
    if provider:
        stmt = stmt.where(KnowledgeDoc.provider == provider)
    if only_stale:
        stmt = stmt.where(KnowledgeDoc.provider != default)

    total = (
        await session.execute(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        )
    ).scalar_one()
    rows = (
        await session.scalars(
            stmt.order_by(desc(KnowledgeDoc.updated_at)).offset(offset).limit(limit)
        )
    ).all()
    stats = await _chunk_stats(session, [r.id for r in rows])
    snippets = await _first_snippets(session, [r.id for r in rows])
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "default_provider": default,
        "items": [
            _doc_item(r, *stats.get(r.id, (0, 0)), snippets.get(r.id, "")) for r in rows
        ],
    }


async def get_knowledge(
    session: AsyncSession, knowledge_id: str, *, chunk_limit: int = 200
) -> dict | None:
    """单篇文档详情：元信息 + 原文 + 分片（最多 ``chunk_limit`` 条）。"""
    doc = await session.get(KnowledgeDoc, knowledge_id)
    if doc is None:
        return None
    rows = (
        await session.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.knowledge_id == knowledge_id)
            .order_by(KnowledgeChunk.id)
            .limit(chunk_limit)
        )
    ).all()
    total = (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.knowledge_id == knowledge_id)
        )
    ).scalar_one()
    return {
        "doc": _doc_item(
            doc,
            total,
            sum(len(r.text) for r in rows),
            rows[0].text if rows else "",
        ),
        "content": doc.content,
        "chunk_total": total,
        "chunks": [
            {
                "id": r.id,
                "index": _chunk_index(r.id, i),
                "source": r.source,
                "text": r.text,
                "dim": len(r.vector or []),
            }
            for i, r in enumerate(rows)
        ],
    }


def _chunk_index(chunk_id: str, fallback: int) -> int:
    """从 ``{doc_id}-c007`` 这种 ID 里解出分片序号；解不出就用位置下标。"""
    _, _, tail = chunk_id.rpartition("-c")
    return int(tail) if tail.isdigit() else fallback


async def update_knowledge(
    session: AsyncSession, knowledge_id: str, payload: KnowledgeDocUpdateRequest
) -> dict | None:
    """改名，和/或替换正文（替换会重新 embedding）。

    文档不存在时返 ``None``，由路由翻译成 404（服务层不抛 HTTP 状态）。

    带 ``content`` 时同录入一样过一道格式归一化（html → Markdown）；
    请求里没写 format 就沿用它现有的格式，只改标题不会动到正文格式。
    """
    doc = await session.get(KnowledgeDoc, knowledge_id)
    if doc is None:
        return None

    if payload.title and payload.title != doc.title:
        doc.title = payload.title
        # 引用里的来源名跟着改，否则旧引用还写着老标题。
        await session.execute(
            update(KnowledgeChunk)
            .where(KnowledgeChunk.knowledge_id == doc.id)
            .values(source=payload.title)
        )

    doc.updated_at = time.time()

    if payload.content is not None:
        # 没传 format 就沿用文档原有格式：编辑器只改一段纯文本时不该被误标成 Markdown
        fmt = payload.format or _doc_format(doc.meta or {})
        body = normalize_content(payload.content, fmt)
        if not body:
            raise ValueError(
                "正文归一化后为空（HTML 里可能只有样式标签），请检查内容或改用 Markdown 录入"
            )
        # 换了正文，旧向量全失效，必须同批重建。
        doc.content = body
        meta = dict(doc.meta or {})
        meta["format"] = stored_format(fmt)
        doc.meta = meta
        await replace_vectors(
            session, doc, body, backend=llm.get_llm(feature="embedding")
        )

    chunks, size = (await _chunk_stats(session, [doc.id])).get(doc.id, (0, 0))
    snippets = await _first_snippets(session, [doc.id])
    return _doc_item(doc, chunks, size, snippets.get(doc.id, ""))


async def reindex_knowledge(session: AsyncSession, knowledge_id: str) -> dict | None:
    """用当前配的 provider 重建一份文档的向量。

    在「系统配置」改了 embedding_provider 之后需要它：已存的向量属于旧
    provider，不重建的话检索永远碰不到它们（列表页靠 ``stale`` 提醒）。
    没存原文的老文档无法重建，直报 ``ValueError``（转 400）请用户重新上传。
    """
    doc = await session.get(KnowledgeDoc, knowledge_id)
    if doc is None:
        return None
    if not doc.content:
        raise ValueError("该文档没有保存原文，无法重建索引，请重新上传文件")
    previous = doc.provider
    backend = llm.get_llm(feature="embedding")
    chunks = await replace_vectors(session, doc, doc.content, backend=backend)
    return {
        "knowledge_id": doc.id,
        "provider": doc.provider,
        "previous_provider": previous,
        "chunks": chunks,
        "chars": len(doc.content),
    }


async def delete_knowledge(session: AsyncSession, knowledge_id: str) -> bool:
    """删一份文档及其全部分片，返是否真的删了（不存在不算错）。

    先删子表再删主表：两边都没外键级联，反序会留下无法回收的孤儿分片。
    图片文件走引用计数回收：还有别的文档/文章引用着就只删本行不删字节。
    """
    doc = await session.get(KnowledgeDoc, knowledge_id)
    if doc is None:
        return False
    image_urls = media_gc.doc_image_urls(doc.meta)
    await session.execute(
        delete(KnowledgeChunk).where(KnowledgeChunk.knowledge_id == knowledge_id)
    )
    await session.delete(doc)
    await session.flush()
    # 行已删才轮得到文件：此刻 metadata 里的 URL 不会再被本行命中
    await media_gc.release_urls(session, image_urls)
    return True


async def delete_knowledge_many(
    session: AsyncSession, ids: list[str]
) -> tuple[int, list[str]]:
    """批量删除，返 ``(删掉数, 找不到的 id)``——不整批回滚，得逐个给结果。"""
    deleted, failed = 0, []
    for kid in ids:
        if await delete_knowledge(session, kid):
            deleted += 1
        else:
            failed.append(kid)
    return deleted, failed


async def _vectorless_chunks(session: AsyncSession) -> int:
    """没可用向量（NULL）的分片数：重建索引前对检索完全隐形。"""
    return (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.vector.is_(None))
        )
    ).scalar_one()


async def knowledge_stats(session: AsyncSession) -> dict:
    """管理页页头的总量统计 + 按 provider 的分布。

    刻意把全表捞到内存里累加：文档量级在百级，而多表聚合在 SQLite
    上反而写不出一句兼容两边的 SQL。量大了再改成单条 GROUP BY。
    """
    settings = get_settings()
    default = _default_provider()
    docs = (await session.scalars(select(KnowledgeDoc))).all()
    stats = await _chunk_stats(session, [d.id for d in docs])

    by_provider: dict[str, tuple[int, int]] = {}
    total_chunks = 0
    total_chars = 0
    stale = 0
    missing_content = 0
    latest = 0.0
    for d in docs:
        chunks, size = stats.get(d.id, (0, 0))
        chars = len(d.content) if d.content else size
        total_chunks += chunks
        total_chars += chars
        stale += d.provider != default
        missing_content += not d.content
        latest = max(latest, d.updated_at or d.ingested_at or 0.0)
        count, size_old = by_provider.get(d.provider, (0, 0))
        by_provider[d.provider] = (count + 1, size_old + chunks)

    return {
        "docs": len(docs),
        "chunks": total_chunks,
        "chars": total_chars,
        "default_provider": default,
        # 模型名与实际调用同源：「模型接入」里的数据库配置优先，.env 兜底
        "embedding_model": llm.embedding_model(default),
        # 向量存在哪、多少维：让前端能说明白「为什么两份文档搜不到」
        "vector_backend": "pgvector" if _uses_pgvector(session) else "json",
        "vector_dim": settings.rag_vector_dim,
        "vectorless_chunks": await _vectorless_chunks(session),
        "stale_docs": stale,
        "missing_content_docs": missing_content,
        "latest_updated_at": latest or None,
        "by_provider": [
            {
                "provider": name,
                "docs": docs_n,
                "chunks": chunks_n,
                "stale": name != default,
            }
            for name, (docs_n, chunks_n) in sorted(
                by_provider.items(), key=lambda kv: (-kv[1][0], kv[0])
            )
        ],
    }


async def _search_pgvector(
    session: AsyncSession,
    doc_ids: Sequence[str],
    query_emb: list[float],
    top_k: int,
) -> list[tuple[str, str, str, float]]:
    """用 pgvector 在库内算余弦距离取 top-k。

    ``<=>`` 是余弦距离，所以 ``1 - distance`` 就是以前 Python 打分器返的那个相似度，
    两条路径的分数口径保持一致。向量为 NULL 的分片（列装不下 / 重建到一半）
    直接跳过而不是记 0 分。查询向量维度不对时报错，不做近似匹配。
    """
    dim = _vector_dim()
    if len(query_emb) != dim:
        raise _dim_error("查询", len(query_emb), dim)
    distance = KnowledgeChunk.vector.cosine_distance(query_emb)
    rows = (
        await session.execute(
            select(
                KnowledgeChunk.knowledge_id,
                KnowledgeChunk.source,
                KnowledgeChunk.text,
                (1.0 - distance).label("score"),
            )
            .where(
                KnowledgeChunk.knowledge_id.in_(doc_ids),
                KnowledgeChunk.vector.is_not(None),
            )
            .order_by(distance)
            .limit(top_k)
        )
    ).all()
    return [(doc_id, source, text, float(score)) for doc_id, source, text, score in rows]


async def _search_in_python(
    session: AsyncSession,
    doc_ids: Sequence[str],
    query_emb: list[float],
    top_k: int,
) -> list[tuple[str, str, str, float]]:
    """SQLite 回退路径：没有向量索引，把候选分片全拉进内存逐个打分。"""
    rows = (
        await session.execute(
            select(
                KnowledgeChunk.knowledge_id,
                KnowledgeChunk.source,
                KnowledgeChunk.text,
                KnowledgeChunk.vector,
            ).where(KnowledgeChunk.knowledge_id.in_(doc_ids))
        )
    ).all()
    scored = [
        (doc_id, source, text, _cosine(query_emb, vector or []))
        for doc_id, source, text, vector in rows
    ]
    scored.sort(key=lambda hit: hit[3], reverse=True)
    return scored[:top_k]


async def _search_lexical_pg(
    session: AsyncSession,
    doc_ids: Sequence[str],
    query: str,
    top_k: int,
) -> list[tuple[str, str, str, float]]:
    """用 pg_trgm 三元组相似度做词法（关键词）召回。

    向量检索旁边的第二条腿：它抓的是精确术语、标识符、低频词——这些 embedding
    容易抹平的东西。用 ``word_similarity``：拿文档里匹配最好的连续窗口去比（通常
    较短的）查询，正好是关键词查法的形状，而且不需分词器就能命中中文
    （``to_tsvector`` 会把不带空格的中文当成单 token，不适合本项目）。

    依赖 ``pg_trgm`` 扩展；调用方包在 try/except 里，没装的库自然退成纯向量。
    """
    if top_k <= 0 or not query.strip():
        return []
    sim = func.word_similarity(query, KnowledgeChunk.text)
    rows = (
        await session.execute(
            select(
                KnowledgeChunk.knowledge_id,
                KnowledgeChunk.source,
                KnowledgeChunk.text,
                sim.label("score"),
            )
            .where(
                KnowledgeChunk.knowledge_id.in_(doc_ids),
                sim > 0.0,
            )
            .order_by(desc("score"))
            .limit(top_k)
        )
    ).all()
    return [(doc_id, source, text, float(score)) for doc_id, source, text, score in rows]


def _rrf_fuse(
    vector_hits: list[tuple[str, str, str, float]],
    lexical_hits: list[tuple[str, str, str, float]],
    *,
    k: int,
    vector_weight: float,
    pool: int,
) -> list[tuple[str, str, str, float]]:
    """用 RRF（倒数排序融合）把向量腿与词法腿并成一个候选池。

    两个入参都是 ``(doc_id, source, text, 本腿得分)``、已按各自指标从好到坏排好。
    RRF 只看*名次*，从而避开余弦相似度与三元组相似度标度不同的问题：

        score(chunk) = Σ_腿 weight_腿 / (k + rank_腿)      # rank 从 1 开始

    两腿都命中的分片会累加从而上浮。融合后的元组里展示用的 ``score`` 优先留
    向量余弦（是个可比的 0..1 数），没有才用词法相似度，否则前端的进度条没法读。
    去重键是 ``doc_id`` + 完整分片文本——同一个分片在两腿里完全一致，所以能正确合并。
    """
    text_of: dict[str, tuple[str, str, str]] = {}  # key -> (doc_id, source, text)
    fused: dict[str, float] = {}
    display: dict[str, float] = {}
    legs = (
        (vector_hits, vector_weight, True),
        (lexical_hits, 1.0 - vector_weight, False),
    )
    for hits, weight, is_vector in legs:
        if weight <= 0:
            continue
        for rank, (doc_id, source, text, leg_score) in enumerate(hits):
            key = f"{doc_id}\x00{text}"
            fused[key] = fused.get(key, 0.0) + weight / (k + rank + 1)
            if key not in text_of:
                text_of[key] = (doc_id, source, text)
            if is_vector:
                display[key] = leg_score  # 展示分优先用余弦
            else:
                display.setdefault(key, leg_score)  # 没有余弦时才退到三元组分
    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[: max(pool, 0)]
    return [
        (*text_of[key], display.get(key, fused_score))
        for key, fused_score in ordered
    ]


async def _rerank_hits(
    query: str, hits: list[tuple[str, str, str, float]], keep: int
) -> list[tuple[str, str, str, float]]:
    """用 cross-encoder 给候选重排，只留最好的 ``keep`` 条。

    ``hits`` 是 ``(doc_id, source, 全量正文, 召回分)``（纯向量，或开了混合检索时的
    RRF 融合结果）。排序跑在*完整*分片文本上（而不是截断后的预览），否则精度会掉。
    任何失败、或精排没开启时，直取向量序的前 ``keep`` 条，保证检索不会因它而坏。
    存下的 ``score`` 仍是余弦相似度（展示语义不变），只有顺序反映精排结果。
    """
    if keep <= 0 or len(hits) <= keep:
        return hits[: max(keep, 0)] if keep > 0 else hits
    ranked = await reranker.rerank(query, [h[2] for h in hits], keep)
    if not ranked:
        return hits[:keep]
    min_score = get_settings().rag_rerank_min_score
    out: list[tuple[str, str, str, float]] = []
    for idx, score in ranked:
        if score < min_score:
            continue
        out.append(hits[idx])
        if len(out) >= keep:
            break
    # 阈值抬得过高可能全部过滤掉，这本身是合法结果（确实没有好匹配）；
    # 但宁可留一条也不让「召回里其实有可用内容」被全滤光而不为人知。
    return out[:keep]


async def search(
    session: AsyncSession,
    payload: KnowledgeSearchRequest,
    *,
    restrict_doc_ids: Sequence[str] | None = None,
) -> KnowledgeSearchResponse:
    """检索主流程：问句转向量 → 召回 →（融合）→（精排）→ 拼引用。

    开关决定三件事：用哪条召回腿（pgvector / 内存 cosine）、要不要叠词法腿、
    要不要精排。只要下游会重排就先宽召回（recall_k），最后再裁到 top_k；
    两个开关都没开时行为与单段式完全一致。

    ``restrict_doc_ids`` 把召回限定在一批文档内（博客前台用它只检已发布文章，
    不混入其他知识文档）；传空列表直接返空结果，不发 embedding 请求。
    """
    if restrict_doc_ids is not None and not restrict_doc_ids:
        return KnowledgeSearchResponse(
            query=payload.query,
            provider=_resolved_provider(payload.provider),
            items=[],
        )

    backend = llm.get_llm(payload.provider, feature="embedding")
    provider = backend.provider
    query_emb = (
        await backend.embeddings(EmbeddingRequest(provider=provider, input=[payload.query]))
    ).items[0].embedding

    # 只检同一个 provider 录入的文档：跨 provider/模型的向量不可比（维度与词表都不同）。
    doc_stmt = select(KnowledgeDoc.id).where(KnowledgeDoc.provider == provider.value)
    if restrict_doc_ids is not None:
        doc_stmt = doc_stmt.where(KnowledgeDoc.id.in_(list(restrict_doc_ids)))
    doc_ids = (await session.scalars(doc_stmt)).all()
    if not doc_ids:
        return KnowledgeSearchResponse(query=payload.query, provider=provider, items=[])

    settings = get_settings()
    on_pg = _uses_pgvector(session)
    rerank_on = settings.rag_rerank_enabled and bool(settings.rag_rerank_base_url)
    # 混合检索需要 Postgres（pg_trgm）；SQLite 静默退成纯向量。
    hybrid_on = settings.rag_hybrid_enabled and on_pg
    # 只要下游（融合/精排）会重排就先宽召回；两者都没开时，这就是原来的单段 top_k。
    recall_k = (
        max(payload.top_k * settings.rag_rerank_recall_multiplier, payload.top_k)
        if (rerank_on or hybrid_on)
        else payload.top_k
    )

    if on_pg:
        vector_hits = await _search_pgvector(session, doc_ids, query_emb, recall_k)
    else:
        vector_hits = await _search_in_python(session, doc_ids, query_emb, recall_k)

    hits = vector_hits
    if hybrid_on:
        # 词法腿自己取一份 top-k；库里没 pg_trgm 扩展时这里查不到东西，检索停在纯向量。
        try:
            lexical_hits = await _search_lexical_pg(
                session,
                doc_ids,
                payload.query,
                max(recall_k, settings.rag_hybrid_text_topk),
            )
        except Exception as exc:  # noqa: BLE001 - trigram missing -> keep vector-only
            logger.warning("词法召回失败，退回纯向量检索：%s", exc)
            lexical_hits = []
        if lexical_hits:
            hits = _rrf_fuse(
                vector_hits,
                lexical_hits,
                k=settings.rag_hybrid_rrf_k,
                vector_weight=settings.rag_hybrid_vector_weight,
                pool=recall_k,
            )

    if rerank_on:
        hits = await _rerank_hits(payload.query, hits, payload.top_k)
    else:
        hits = hits[: payload.top_k]

    # 把存储的图片（图片类文档 / 文档内插图）附在结果上，让调用方能*展示*它们。
    # 可供检索的文字描述已经在正文分片里，图片只是旁挂，取自
    # KnowledgeDoc.meta["images"]（/media/uploads 的 URL，或历史遗留的 data URL）。
    doc_images: dict[str, list[str]] = {}
    matched_ids = {doc_id for doc_id, *_ in hits}
    if matched_ids:
        meta_rows = (
            await session.execute(
                select(KnowledgeDoc.id, KnowledgeDoc.meta).where(
                    KnowledgeDoc.id.in_(matched_ids)
                )
            )
        ).all()
        for did, meta in meta_rows:
            imgs = (meta or {}).get("images") or []
            urls = [u for u in imgs if isinstance(u, str) and u]
            if urls:
                doc_images[did] = urls
    items = [
        Citation(
            index=i,
            knowledge_id=doc_id,
            source=source,
            score=score,
            snippet=text[:SNIPPET_CHARS],
            images=doc_images.get(doc_id, []),
        )
        for i, (doc_id, source, text, score) in enumerate(hits)
    ]
    return KnowledgeSearchResponse(query=payload.query, provider=provider, items=items)


RAG_SYSTEM_PROMPT = (
    "你是一个知识库问答助手。请基于下方提供的检索片段（Context）作答，"
    "并遵守以下要求：\n"
    "1. 先理解并整合这些片段，用你自己的话组织成条理清晰、结构化的回答，"
    "不要简单拼接或整段照抄原文；\n"
    "2. 内容较长时用 Markdown 组织：用小标题（### ）分段、用要点列表（- 或 1. ）罗列，"
    "突出关键结论；\n"
    "3. 引用了某个片段的结论时在句末标注来源编号 [n]（对应 Context 中的编号）；\n"
    "4. 只依据 Context 作答，Context 里没有的信息要明确说明「知识库中未找到相关内容」，不要编造。"
)


async def answer(
    session: AsyncSession, payload: KnowledgeAnswerRequest
) -> KnowledgeAnswerResponse:
    """知识库问答：先 :func:`search` 取片段，拼 Context 后交给对话模型。"""
    backend = llm.get_llm(payload.provider, feature="rag")
    provider = backend.provider
    search_resp = await search(
        session,
        # 检索用的是向量 provider，不能跟着回答模型走：把请求里显式指定的那个传下去，
        # 没指定时由 search() 自己按 embedding_provider 解析（否则改了 rag_provider
        # 就会查不到任何文档——向量不在同一个 provider 命名空间里）。
        KnowledgeSearchRequest(
            query=payload.query, top_k=payload.top_k, provider=payload.provider
        ),
    )
    context = "\n".join(f"[{c.index}] {c.snippet}" for c in search_resp.items)
    question = f"Context:\n{context}\n\nQuestion: {payload.query}"
    # 多模态：调用方带了图（base64 data URL 或 http URL）就把它与文本一起发出去，
    # 让视觉模型能看图作答；纯文本模型会在上游忽略或拒收这些 parts。
    if payload.images:
        user_content = [
            {"type": "text", "text": question},
            *(
                {"type": "image_url", "image_url": {"url": url}}
                for url in payload.images
                if url
            ),
        ]
    else:
        user_content = question
    content, _finish, usage, _model = await backend.chat(
        [
            ChatMessage(role="system", content=RAG_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_content),
        ]
    )
    return KnowledgeAnswerResponse(
        query=payload.query,
        provider=provider,
        answer=content,
        citations=search_resp.items,
        usage=usage,
    )
