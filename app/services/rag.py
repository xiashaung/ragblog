"""Retrieval-augmented generation.

Chunking + a vector store that lives in the app's own database. On Postgres
the vectors are a real pgvector column (``vector(<APP_RAG_VECTOR_DIM>)``, so
cosine distance and top-k are computed by the database and served from an HNSW
index); on SQLite the same column degrades to JSON and scoring falls back to
Python, which keeps the test suite and lightweight dev setups working without
an extension.

Embeddings can come from any configured provider (see ``llm.get_llm``); the
chosen provider name is recorded per knowledge document so searches can be
scoped to documents ingested with a specific provider.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Sequence

from sqlalchemy import delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

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

DEFAULT_CHUNK_SIZE = 500
PREVIEW_CHARS = 120
SNIPPET_CHARS = 500
# FAQ markers: the question leads each block so embeddings match on it.
FAQ_ASK_PREFIX = "问："
FAQ_ANSWER_PREFIX = "答："

logger = logging.getLogger(__name__)


def _chunk_size_of(
    requested: int | None, meta: dict | None = None, fallback: int = DEFAULT_CHUNK_SIZE
) -> int:
    """Resolve the chunk size: request override > stored setting > default."""
    if requested:
        return requested
    stored = (meta or {}).get("chunk_size")
    return stored if isinstance(stored, int) and stored > 0 else fallback


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 80) -> list[str]:
    """Split text into overlapping chunks at paragraph boundaries.

    Paragraphs are packed until ``chunk_size``; any single piece longer than
    that is hard-split first, so a document without newlines (a CSV, a log,
    CJK prose) still yields bounded chunks instead of one giant one. Every
    returned chunk is at most ``chunk_size`` chars, overlap tail included.
    """
    overlap = max(0, min(overlap, chunk_size // 2))
    # Hard-split width: an overlap tail prepended at the next flush still
    # fits inside ``chunk_size``.
    width = max(1, chunk_size - overlap - 1)

    pieces: list[str] = []
    for para in (p.strip() for p in text.split("\n")):
        if not para:
            continue
        if len(para) <= width:
            pieces.append(para)
            continue
        step = max(1, width - overlap)
        pieces.extend(para[start : start + width] for start in range(0, len(para), step))

    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        if buffer and len(buffer) + len(piece) + 1 > chunk_size:
            chunks.append(buffer)
            # keep the tail for overlap
            buffer = f"{buffer[-overlap:]} {piece}".strip() if overlap else piece
        else:
            buffer = f"{buffer}\n{piece}".strip() if buffer else piece
    if buffer:
        chunks.append(buffer)
    return chunks or [text[:chunk_size]]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _resolved_provider(name: Provider | str | None) -> Provider:
    """The provider ``get_llm`` will use for this request.

    Reads the backend's resolved ``provider`` attribute so the response can
    echo it back (matching what the backend actually used).
    """
    return llm.get_llm(name).provider


def _default_provider() -> str:
    """Provider name the frontend now gets when it omits ``provider``."""
    return llm.get_llm(None).provider.value


def _vector_dim() -> int:
    """Column dimension every stored vector must match."""
    return get_settings().rag_vector_dim


def _uses_pgvector(session: AsyncSession) -> bool:
    """True when ``vector`` is a real pgvector column (i.e. on Postgres)."""
    return session.get_bind().dialect.name == "postgresql"


def _dim_error(who: str, got: int, wanted: int) -> ValueError:
    """The one place that explains a dimension mismatch to the user."""
    return ValueError(
        f"{who}的向量是 {got} 维，与知识库列维度 {wanted} 不一致。"
        "若是换了 embedding 模型：改 APP_RAG_VECTOR_DIM 并重跑 alembic upgrade head，"
        "再对文档重建索引"
    )


def _doc_kind(doc: KnowledgeDoc) -> str:
    """Where the document came from: file / text / faq / url."""
    kind = (doc.meta or {}).get("kind")
    if isinstance(kind, str) and kind:
        return kind
    return "file" if (doc.meta or {}).get("source_file") else "text"


def _doc_item(doc: KnowledgeDoc, chunks: int, chars: int, first_text: str = "") -> dict:
    """Management-list shape for a document (no chunk bodies)."""
    default = _default_provider()
    content = doc.content or ""
    meta = doc.meta or {}
    # Legacy rows predate the ``content`` column; fall back to the first chunk
    # so the list still has something to preview.
    source = content or first_text
    return {
        "knowledge_id": doc.id,
        "title": doc.title,
        "provider": doc.provider,
        "chunks": chunks,
        "chars": len(content) or chars,
        "preview": source[:PREVIEW_CHARS].replace("\n", " ").strip(),
        "has_content": bool(content),
        "source_file": meta.get("source_file"),
        "kind": _doc_kind(doc),
        "source_url": meta.get("source_url"),
        "metadata": doc.meta,
        "ingested_at": doc.ingested_at,
        "updated_at": doc.updated_at or doc.ingested_at,
        "stale": doc.provider != default,
    }


async def _first_snippets(session: AsyncSession, doc_ids: list[str]) -> dict[str, str]:
    """First chunk text per document (bounded by one row per doc)."""
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
    """Chunk count + total chunk length per document, without loading texts."""
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
    """Drop the document's chunks and rebuild them (and their vectors).

    Also used for re-indexing: embeddings of different providers / models are
    not comparable, so changing the embedding model means rewriting vectors.
    """
    chunk_size = _chunk_size_of(None, doc.meta)
    chunks = chunk_text(content, chunk_size=chunk_size)
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
    """Same title == same id, so re-ingesting replaces the document."""
    return f"kb-{hashlib.md5(title.encode()).hexdigest()[:12]}"


async def upsert_document(
    session: AsyncSession,
    *,
    title: str,
    content: str,
    provider: Provider | str | None = None,
    chunk_size: int | None = None,
    metadata: dict | None = None,
) -> KnowledgeIngestResponse:
    """Store (or replace) a document and (re)build its chunk vectors.

    Every ingest entry point - file upload, raw text, Q&A pairs, URL - funnels
    through here, so they cannot drift apart.
    """
    backend = llm.get_llm(provider)
    doc_id = _doc_id_for(title)

    existing = await session.get(KnowledgeDoc, doc_id)
    if existing is not None:
        await delete_knowledge(session, existing.id)

    meta = dict(metadata or {})
    if chunk_size:
        meta["chunk_size"] = chunk_size
    doc = KnowledgeDoc(
        id=doc_id,
        title=title,
        provider=backend.provider.value,
        content=content,
        meta=meta or None,
    )
    session.add(doc)
    await session.flush()

    chunks = await replace_vectors(session, doc, content, backend=backend)
    return KnowledgeIngestResponse(
        knowledge_id=doc.id,
        provider=backend.provider,
        chunks=chunks,
        chars=len(content),
    )


async def ingest(
    session: AsyncSession, payload: KnowledgeIngestRequest
) -> KnowledgeIngestResponse:
    return await upsert_document(
        session,
        title=payload.title,
        content=payload.content,
        provider=payload.provider,
        chunk_size=payload.chunk_size,
        metadata=payload.metadata,
    )


def pairs_to_text(pairs: list[tuple[str, str]]) -> str:
    """Render Q&A pairs as blank-line-separated blocks.

    The question leads each block, so a chunk's embedding is dominated by the
    phrasing users actually type when searching.
    """
    return "\n\n".join(
        f"{FAQ_ASK_PREFIX}{question}\n{FAQ_ANSWER_PREFIX}{answer}"
        for question, answer in pairs
    )


async def ingest_pairs(
    session: AsyncSession, payload: KnowledgePairsIngestRequest
) -> KnowledgeIngestResponse:
    """Ingest a FAQ batch as one document."""
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
    """Fetch a page, then ingest its text as a document titled after the page."""
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
    """Paginated document list for the management UI."""
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
    """Single document: metadata, original text and its chunks."""
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
    """Chunk order as encoded in ``{doc_id}-c007``; positional otherwise."""
    _, _, tail = chunk_id.rpartition("-c")
    return int(tail) if tail.isdigit() else fallback


async def update_knowledge(
    session: AsyncSession, knowledge_id: str, payload: KnowledgeDocUpdateRequest
) -> dict | None:
    """Rename, and/or replace the text (which re-embeds it).

    Returns ``None`` when the document does not exist.
    """
    doc = await session.get(KnowledgeDoc, knowledge_id)
    if doc is None:
        return None

    if payload.title and payload.title != doc.title:
        doc.title = payload.title
        # Keep citations pointing at the new name.
        await session.execute(
            update(KnowledgeChunk)
            .where(KnowledgeChunk.knowledge_id == doc.id)
            .values(source=payload.title)
        )

    doc.updated_at = time.time()

    if payload.content is not None:
        # Replacing the text invalidates the vectors, so rebuild them too.
        doc.content = payload.content
        await replace_vectors(
            session, doc, payload.content, backend=llm.get_llm(None)
        )

    chunks, size = (await _chunk_stats(session, [doc.id])).get(doc.id, (0, 0))
    snippets = await _first_snippets(session, [doc.id])
    return _doc_item(doc, chunks, size, snippets.get(doc.id, ""))


async def reindex_knowledge(session: AsyncSession, knowledge_id: str) -> dict | None:
    """Rebuild a document's vectors with the currently configured provider.

    Needed after ``APP_LLM_DEFAULT_PROVIDER`` changes: stored vectors stay
    scoped to the old provider and are invisible to search otherwise.
    """
    doc = await session.get(KnowledgeDoc, knowledge_id)
    if doc is None:
        return None
    if not doc.content:
        raise ValueError("该文档没有保存原文，无法重建索引，请重新上传文件")
    previous = doc.provider
    backend = llm.get_llm(None)
    chunks = await replace_vectors(session, doc, doc.content, backend=backend)
    return {
        "knowledge_id": doc.id,
        "provider": doc.provider,
        "previous_provider": previous,
        "chunks": chunks,
        "chars": len(doc.content),
    }


async def delete_knowledge(session: AsyncSession, knowledge_id: str) -> bool:
    doc = await session.get(KnowledgeDoc, knowledge_id)
    if doc is None:
        return False
    await session.execute(
        delete(KnowledgeChunk).where(KnowledgeChunk.knowledge_id == knowledge_id)
    )
    await session.delete(doc)
    await session.flush()
    return True


async def delete_knowledge_many(
    session: AsyncSession, ids: list[str]
) -> tuple[int, list[str]]:
    """Batch delete; returns (deleted_count, ids that were not found)."""
    deleted, failed = 0, []
    for kid in ids:
        if await delete_knowledge(session, kid):
            deleted += 1
        else:
            failed.append(kid)
    return deleted, failed


async def _vectorless_chunks(session: AsyncSession) -> int:
    """Chunks with no usable vector (NULL): invisible to search until re-indexed."""
    return (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.vector.is_(None))
        )
    ).scalar_one()


async def knowledge_stats(session: AsyncSession) -> dict:
    """Totals for the management header + per-provider breakdown."""
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
        "embedding_model": settings.provider_config(default).embedding_model,
        # 向量存在哪、多少维：让前端能说明白“为什么两份文档搜不到”
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
    """Top-k by cosine distance, computed by pgvector inside the database.

    ``<=>`` is the cosine distance, so ``1 - distance`` is the similarity the
    old in-Python scorer returned. Null vectors (a vector the column could not
    hold, or a half-finished re-index) are skipped rather than scored 0.
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
    """SQLite fallback: no vector index, so score every chunk in memory."""
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
    """Lexical (keyword) recall via pg_trgm trigram similarity.

    The second retrieval leg next to the vector search: it catches exact terms,
    identifiers and rare words that embeddings can smooth over. Uses
    ``word_similarity``, which scores the best-matching contiguous window of a
    document against the (usually shorter) query - the right shape for keyword
    lookup, and it works on CJK without a tokenizer (unlike ``to_tsvector``,
    which treats space-less Chinese as a single token).

    Requires the ``pg_trgm`` extension; callers run it inside a try/except so a
    database without it simply degrades to vector-only retrieval.
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
    """Reciprocal Rank Fusion of the vector and lexical rankings into one pool.

    Each input list is ``(doc_id, source, text, leg_score)`` already ordered
    best-first by its own metric. RRF uses only *rank*, sidestepping the fact
    that cosine similarity and trigram similarity live on different scales::

        score(chunk) = sum_legs weight_leg / (k + rank_leg)      # rank is 1-based

    A chunk recalled by both legs accumulates and floats up. The fused tuple
    keeps the vector cosine as its display ``score`` when present (a comparable
    0..1 number), else the lexical similarity, so the UI's progress bar stays
    meaningful. Keyed by ``doc_id`` + full chunk text - identical across both
    legs for the same chunk, so overlapping recall merges correctly.
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
                display[key] = leg_score  # cosine preferred for display
            else:
                display.setdefault(key, leg_score)  # trigram only if no cosine
    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[: max(pool, 0)]
    return [
        (*text_of[key], display.get(key, fused_score))
        for key, fused_score in ordered
    ]


async def _rerank_hits(
    query: str, hits: list[tuple[str, str, str, float]], keep: int
) -> list[tuple[str, str, str, float]]:
    """Re-order candidate hits with a cross-encoder, keeping the best ``keep``.

    ``hits`` are ``(doc_id, source, full_text, recall_score)`` (vector-only, or
    RRF-fused when hybrid retrieval is on). Ranking runs on the *full* chunk text
    (not the truncated snippet) for accuracy. On any
    failure or when disabled, falls back to the top ``keep`` in vector order so
    retrieval never breaks. The stored ``score`` stays the cosine similarity
    (display semantics unchanged); only the ordering reflects the rerank.
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
    # A high threshold can drop everything; that is legitimate (no good match),
    # but never let an all-filtered result hide a decent recall hit entirely.
    return out[:keep]


async def search(
    session: AsyncSession, payload: KnowledgeSearchRequest
) -> KnowledgeSearchResponse:
    backend = llm.get_llm(payload.provider)
    provider = backend.provider
    query_emb = (
        await backend.embeddings(EmbeddingRequest(provider=provider, input=[payload.query]))
    ).items[0].embedding

    # Only search documents ingested with this provider (vectors are not
    # comparable across providers / models — different dimensions & vocab).
    doc_ids = (
        await session.scalars(
            select(KnowledgeDoc.id).where(KnowledgeDoc.provider == provider.value)
        )
    ).all()
    if not doc_ids:
        return KnowledgeSearchResponse(query=payload.query, provider=provider, items=[])

    settings = get_settings()
    on_pg = _uses_pgvector(session)
    rerank_on = settings.rag_rerank_enabled and bool(settings.rag_rerank_base_url)
    # Hybrid needs Postgres (pg_trgm); SQLite silently stays vector-only.
    hybrid_on = settings.rag_hybrid_enabled and on_pg
    # Wide recall whenever a downstream stage (fusion / rerank) will re-rank it;
    # when neither is on this is exactly the old single-stage top_k.
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
        # Lexical leg pulls its own top-k; a database without the pg_trgm
        # extension yields nothing here and retrieval stays vector-only.
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

    # Attach stored images (uploaded image docs / document-embedded figures) so the
    # caller can *display* them next to the retrieved text. Their textual
    # descriptions already live in the searchable body; the images are referenced
    # from KnowledgeDoc.meta["images"] (a /media/uploads URL, or a legacy data URL).
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
    backend = llm.get_llm(payload.provider)
    provider = backend.provider
    search_resp = await search(
        session,
        KnowledgeSearchRequest(
            query=payload.query, top_k=payload.top_k, provider=provider
        ),
    )
    context = "\n".join(f"[{c.index}] {c.snippet}" for c in search_resp.items)
    question = f"Context:\n{context}\n\nQuestion: {payload.query}"
    # Multimodal: when the caller attaches images (base64 data URL or http URL),
    # send them as content parts alongside the text so a vision model can reason
    # over them. Plain-text-only models ignore / reject these parts upstream.
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
