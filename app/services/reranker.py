"""cross-encoder 精排客户端。

封装一个 OpenAI/Cohere 风格的 ``POST {base}/rerank`` 端点（Xinference、
SiliconFlow、TEI 兼容层等都提供）。分工：向量库做宽召回，本模块把它变成
精确排序——把（查询, 文档）成对送进模型一起打分，而不是各自算向量。

返 ``[(原下标, 相关度分), ...]`` 按分降序；精排关闭 / 不可用时返 ``None``，
调用方接着用原来的余弦序——精排服务挂掉绝不能把检索一并带坑。
端点、模型名、开关都以数据库为准（``provider_store.get_rerank_config()``）。
"""

from __future__ import annotations

import logging

import httpx

from ..config import get_settings
from . import provider_store

logger = logging.getLogger(__name__)

# 每篇文档只给模型看这么多字符：reranker 上下文窗口小，长分片的尾部很少会改变它的名次。
RERANK_DOC_CHARS = 2000


async def rerank(
    query: str, documents: list[str], top_n: int
) -> list[tuple[int, float]] | None:
    """按与 ``query`` 的相关度给 ``documents`` 排序。

    最多返 ``top_n`` 个 ``(下标, 分数)``（下标指回入参列表），好的在前。
    返 ``None`` 表示「本次没排」（没开、没候选、或任何错误），调用方保持原序。
    分数的标度因模型而异，所以上层只用它排序与卡 ``rag_rerank_min_score``，不拿去展示。
    """
    settings = get_settings()
    # 开关与端点都以数据库为准（「系统配置」/「模型管理」），.env 兜底
    rerank_cfg = provider_store.get_rerank_config()
    if not rerank_cfg.enabled or not documents or not rerank_cfg.base_url:
        return None

    base = rerank_cfg.base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if rerank_cfg.api_key:
        headers["Authorization"] = f"Bearer {rerank_cfg.api_key}"
    payload = {
        "model": rerank_cfg.model,
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

    # Cohere/Xinference 用 "results"，有些服务回 "data"；两边都带
    # {index, relevance_score|score}。这里故意取得宽：字段命名差异不应该
    # 造成「精排静默失效」这种难查的问题。
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
