"""Cross-encoder rerank client.

A thin async caller for an OpenAI/Cohere-style ``POST {base}/rerank`` endpoint
(the shape served by Xinference, SiliconFlow, TEI-compatible layers, etc). The
vector store does broad recall; this turns that into a precise ordering by
scoring each ``(query, document)`` pair jointly.

Returns ``[(original_index, relevance_score), ...]`` sorted by score desc, or
``None`` when reranking is disabled / unavailable — callers then fall back to
the original cosine order, so a missing rerank server never breaks retrieval.
"""

from __future__ import annotations

import logging

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

# Per-document character cap sent to the model: rerankers have small context
# windows and the tail of a long chunk rarely changes its relevance ranking.
RERANK_DOC_CHARS = 2000


async def rerank(
    query: str, documents: list[str], top_n: int
) -> list[tuple[int, float]] | None:
    """Rank ``documents`` by relevance to ``query``.

    Returns at most ``top_n`` ``(index, score)`` pairs (index into the input
    list), best first. Returns ``None`` to signal "not applied" (disabled, no
    candidates, or any error) — the caller keeps its existing order.
    """
    settings = get_settings()
    if not settings.rag_rerank_enabled or not documents or not settings.rag_rerank_base_url:
        return None

    base = settings.rag_rerank_base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if settings.rag_rerank_api_key:
        headers["Authorization"] = f"Bearer {settings.rag_rerank_api_key}"
    payload = {
        "model": settings.rag_rerank_model,
        "query": query,
        "documents": [d[:RERANK_DOC_CHARS] for d in documents],
        "top_n": min(max(top_n, 1), len(documents)),
    }

    try:
        async with httpx.AsyncClient(timeout=settings.rag_rerank_timeout) as client:
            resp = await client.post(f"{base}/rerank", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 - any failure must degrade to vector order
        logger.warning("rerank 调用失败，回退向量排序：%s", exc)
        return None

    # Cohere/Xinference use "results"; some servers return "data". Both carry
    # {index, relevance_score|score}. Be liberal so a provider difference in
    # field naming does not silently disable reranking.
    results = data.get("results") or data.get("data") or []
    ranked: list[tuple[int, float]] = []
    for item in results:
        idx = item.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(documents)):
            continue
        score = item.get("relevance_score", item.get("score", 0.0))
        try:
            ranked.append((idx, float(score)))
        except (TypeError, ValueError):
            ranked.append((idx, 0.0))
    if not ranked:
        return None
    ranked.sort(key=lambda kv: kv[1], reverse=True)
    return ranked
