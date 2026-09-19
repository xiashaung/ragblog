"""AI endpoints: chat, embeddings, RAG knowledge base.

All AI endpoints accept an optional ``provider`` field; when omitted they
fall back to ``APP_LLM_DEFAULT_PROVIDER``. Supported:
``openai`` / ``deepseek`` / ``qwen`` / ``ollama`` / ``mlx`` / ``mock``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .. import deps, services
from ..config import Provider, get_settings
from ..db import get_db
from ..schemas.ai import (
    ChatAppendRequest,
    ChatAppendResponse,
    ChatChunk,
    ChatMessageListResponse,
    ChatRequest,
    ChatResponse,
    ChatSessionCreate,
    ChatSessionListResponse,
    ChatSessionOut,
    ChatSessionUpdate,
    EmbeddingRequest,
    EmbeddingResponse,
    KnowledgeAnswerChunk,
    KnowledgeAnswerRequest,
    KnowledgeAnswerResponse,
    KnowledgeBatchDeleteRequest,
    KnowledgeBatchDeleteResponse,
    KnowledgeDetailResponse,
    KnowledgeDocItem,
    KnowledgeDocUpdateRequest,
    KnowledgeFetchRequest,
    KnowledgeFetchResponse,
    KnowledgeIngestRequest,
    KnowledgeIngestResponse,
    KnowledgeListResponse,
    KnowledgePairsIngestRequest,
    KnowledgeReindexResponse,
    KnowledgeReplaceVectorsRequest,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeStatsResponse,
    Usage,
    WebSource,
)
from ..services.llm import get_llm
from ..services.rag_queue import TaskType, get_queue

router = APIRouter()
logger = logging.getLogger(__name__)


def _resolve_provider(name: Provider | str | None) -> Provider:
    if name is None:
        return None
    return name if isinstance(name, Provider) else Provider(name)


@router.get("/providers", response_model=list[dict])
async def list_providers() -> list[dict]:
    """Expose provider defaults so the frontend can pre-fill its selectors
    and warn when a real provider has no API key configured.

    Returns a bare JSON array on purpose: the envelope middleware only wraps
    object bodies, so a ``{"data": [...]}`` dict would reach the client as a
    doubly-nested payload and break every ``providers.filter(...)`` call.
    """
    settings = get_settings()
    out = []
    for name in Provider:
        cfg = settings.provider_config(name.value)
        out.append(
            {
                "name": name.value,
                "base_url": cfg.base_url,
                "chat_model": cfg.chat_model,
                "embedding_model": cfg.embedding_model,
                "api_key_configured": bool(cfg.api_key)
                or name.value in ("ollama", "mlx", "mock"),
            }
        )
    out.sort(key=lambda p: (p["name"] != settings.llm_default_provider, str(p["name"])))
    return out


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    _session: AsyncSession = Depends(get_db),
) -> ChatResponse:
    # ``provider=None`` falls back to ``APP_LLM_DEFAULT_PROVIDER`` inside
    # ``get_llm``; the resolved value is read back from the backend.
    llm = get_llm(req.provider)
    if req.stream:
        return await _stream_chat(req)
    # 非流式走同一个工具循环，只是把块攒起来一次返回。刻意的：另写一条
    # ``llm.chat`` 直达路径会让“联网”只在流式可用，同一个接口两种行为比难查。
    parts: list[str] = []
    # 思考增量：流式下是一块块发的，非流式要自己拼回去（与 ``content`` 互不混入）
    think: list[str] = []
    usage: Usage | None = None
    finish: str | None = None
    sources: list[WebSource] = []
    seen: set[str] = set()
    async for chunk in _turn_stream(llm, req):
        if chunk.content:
            parts.append(chunk.content)
        if chunk.reasoning:
            think.append(chunk.reasoning)
        if chunk.usage:
            usage = chunk.usage
        if chunk.finish_reason:
            finish = chunk.finish_reason
        if chunk.tool and chunk.tool.phase == "result":
            # 按 URL 去重：多轮搜索常命中同一条，引用列表重复出来没法看
            for src in chunk.tool.sources:
                if src.url in seen:
                    continue
                seen.add(src.url)
                sources.append(src)
        if chunk.error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, chunk.error)
    return ChatResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
        provider=llm.provider,
        model=req.model or get_settings().provider_config(llm.provider.value).chat_model,
        content="".join(parts),
        reasoning="".join(think),
        finish_reason=finish,
        usage=usage,
        web_sources=sources,
    )


def _turn_stream(llm: object, req: ChatRequest):
    """统一的“一轮问答”生成器（含工具循环），流式与非流式共用。"""
    return services.chat_tools.stream_with_tools(
        llm,
        messages=list(req.messages),
        model=req.model,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        response_format=req.response_format,
        web_mode=req.web_mode,
    )


async def _stream_chat(req: ChatRequest) -> StreamingResponse:
    llm = get_llm(req.provider)

    async def gen():
        # 逐行边生边发（旧的 await 成 list 写法会把流式退化成“等全部生成完”）
        try:
            async for c in _turn_stream(llm, req):
                yield json.dumps(c.model_dump(), ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001 - 流已开始，只能把错误包成最后一行 chunk
            logger.exception("流式对话中断")
            tail = ChatChunk(done=True, error=f"生成中断：{exc}")
            yield json.dumps(tail.model_dump(), ensure_ascii=False) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no"},
    )


# ---------------- 对话会话持久化 ----------------
# 会话是登录者自己的对话记录，不属于 RBAC 管辖的功能权限：只要登录态
# （require_admin_session）即可，不必为对话历史再增加一个所有人都要授予的权限码。
# 跨身份的会话 id 一律 404（服务层已把“不存在”与“不是你的”合并）。


@router.get("/chat/sessions", response_model=ChatSessionListResponse)
async def list_chat_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
    admin_id: str = Depends(deps.require_admin_session),
) -> ChatSessionListResponse:
    """本人的会话列表（最近活跃的在前，附消息条数）。"""
    return await services.chat_history.list_sessions(session, admin_id, limit=limit)


@router.post("/chat/sessions", response_model=ChatSessionOut)
async def create_chat_session(
    req: ChatSessionCreate,
    session: AsyncSession = Depends(get_db),
    admin_id: str = Depends(deps.require_admin_session),
) -> ChatSessionOut:
    """新建空会话；标题留空时用占位名，首条提问入库后自动改名。"""
    return await services.chat_history.create_session(session, admin_id, req.title)


@router.patch("/chat/sessions/{chat_id}", response_model=ChatSessionOut)
async def rename_chat_session(
    chat_id: str,
    req: ChatSessionUpdate,
    session: AsyncSession = Depends(get_db),
    admin_id: str = Depends(deps.require_admin_session),
) -> ChatSessionOut:
    """会话改名（仅本人的）。"""
    out = await services.chat_history.rename_session(session, admin_id, chat_id, req.title)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return out


@router.delete("/chat/sessions/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat_session(
    chat_id: str,
    session: AsyncSession = Depends(get_db),
    admin_id: str = Depends(deps.require_admin_session),
) -> None:
    """删除会话及其全部消息（仅本人的）。"""
    deleted = await services.chat_history.delete_session(session, admin_id, chat_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")


@router.get(
    "/chat/sessions/{chat_id}/messages", response_model=ChatMessageListResponse
)
async def list_chat_messages(
    chat_id: str,
    session: AsyncSession = Depends(get_db),
    admin_id: str = Depends(deps.require_admin_session),
) -> ChatMessageListResponse:
    """会话内的消息（时间正序），用于刷新后回放对话流。"""
    out = await services.chat_history.get_messages(session, admin_id, chat_id)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return out


@router.post(
    "/chat/sessions/{chat_id}/messages", response_model=ChatAppendResponse
)
async def append_chat_messages(
    chat_id: str,
    req: ChatAppendRequest,
    session: AsyncSession = Depends(get_db),
    admin_id: str = Depends(deps.require_admin_session),
) -> ChatAppendResponse:
    """追加一轮问答（用户消息 + 助手消息）。

    流式接口里不写库（生成器活过请求级 DB session），所以由前端在一轮生成
    **完成后**调用本接口入库；失败的半截回答不入历史，避免回放时混入噪声。
    """
    out = await services.chat_history.append_messages(
        session, admin_id, chat_id, req.messages
    )
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return out


@router.post("/embeddings", response_model=EmbeddingResponse)
async def embeddings(
    req: EmbeddingRequest,
    _session: AsyncSession = Depends(get_db),
) -> EmbeddingResponse:
    return await get_llm(req.provider).embeddings(req)


# --- RAG / knowledge base ---
#
# Static paths (``/knowledge/stats``, ``/knowledge/ingest*``, ``/knowledge/fetch-url``)
# must stay declared before ``/knowledge/{knowledge_id}``, otherwise the path
# parameter swallows them.


@router.post("/knowledge/ingest", response_model=KnowledgeIngestResponse)
async def ingest(
    req: KnowledgeIngestRequest,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeIngestResponse:
    """文档入库（异步）：立即返回任务 ID，实际处理在后台队列执行。"""
    # 测试环境使用同步模式
    sync_mode = os.environ.get("APP_TEST_MODE", "false").lower() == "true"
    
    if sync_mode:
        # 测试中直接执行并返回结果
        result = await services.rag.ingest(session, req)
        return KnowledgeIngestResponse(
            knowledge_id=result.knowledge_id,
            provider=result.provider,
            chunks=result.chunks,
            chars=result.chars,
            task_id=None,
        )
    else:
        # 生产环境异步入队
        queue = get_queue()
        task_id = await queue.enqueue(
            services.rag_queue.RAGTask(
                task_type=TaskType.INGEST,
                payload=req.model_dump(),
                created_at=time.time(),
            ),
            sync=False,
        )
        return KnowledgeIngestResponse(
            knowledge_id=f"pending-{task_id}",
            provider=req.provider or get_settings().llm_default_provider,
            chunks=0,
            chars=0,
            task_id=task_id,
        )


@router.post("/knowledge/ingest-pairs", response_model=KnowledgeIngestResponse)
async def ingest_pairs(
    req: KnowledgePairsIngestRequest,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeIngestResponse:
    """录入一批问答对（FAQ），合为一篇文档重建索引（异步）。"""
    queue = get_queue()
    task_id = await queue.enqueue(
        services.rag_queue.RAGTask(
            task_type=TaskType.INGEST_PAIRS,
            payload=req.model_dump(),
            created_at=time.time(),
        )
    )
    return KnowledgeIngestResponse(
        knowledge_id=f"pending-{task_id}",
        provider=req.provider or get_settings().llm_default_provider,
        chunks=0,
        chars=0,
        task_id=task_id,
    )


@router.post("/knowledge/fetch-url", response_model=KnowledgeFetchResponse)
async def ingest_from_url(
    req: KnowledgeFetchRequest,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeFetchResponse:
    """抓取一个网页地址，抽正文后入库（异步）。"""
    queue = get_queue()
    task_id = await queue.enqueue(
        services.rag_queue.RAGTask(
            task_type=TaskType.INGEST_URL,
            payload=req.model_dump(),
            created_at=time.time(),
        )
    )
    # 立即返回接受状态
    return KnowledgeFetchResponse(
        knowledge_id=f"pending-{task_id}",
        provider=req.provider or get_settings().llm_default_provider,
        chunks=0,
        chars=0,
        title=req.title or "",
        url=req.url,
        final_url=None,
        page_chars=0,
        truncated=False,
        task_id=task_id,
    )


@router.get("/knowledge/stats", response_model=KnowledgeStatsResponse)
async def knowledge_stats(
    session: AsyncSession = Depends(get_db),
) -> KnowledgeStatsResponse:
    """Document / chunk totals plus per-provider breakdown."""
    return await services.rag.knowledge_stats(session)


@router.get("/knowledge", response_model=KnowledgeListResponse)
async def list_knowledge(
    keyword: str | None = Query(default=None, description="按标题 / 正文模糊匹配"),
    provider: str | None = Query(default=None, description="按入库 provider 过滤"),
    only_stale: bool = Query(default=False, description="只看与当前默认 provider 不一致的文档"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
) -> KnowledgeListResponse:
    return await services.rag.list_knowledge(
        session,
        keyword=keyword,
        provider=provider,
        only_stale=only_stale,
        offset=offset,
        limit=limit,
    )


@router.post("/knowledge/batch-delete", response_model=KnowledgeBatchDeleteResponse)
async def batch_delete_knowledge(
    req: KnowledgeBatchDeleteRequest,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeBatchDeleteResponse:
    deleted, failed = await services.rag.delete_knowledge_many(session, req.ids)
    return KnowledgeBatchDeleteResponse(deleted=deleted, failed=failed)


@router.get("/knowledge/{knowledge_id}", response_model=KnowledgeDetailResponse)
async def knowledge_detail(
    knowledge_id: str,
    chunk_limit: int = Query(default=200, ge=1, le=2000),
    session: AsyncSession = Depends(get_db),
) -> KnowledgeDetailResponse:
    detail = await services.rag.get_knowledge(
        session, knowledge_id, chunk_limit=chunk_limit
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    return detail


@router.patch("/knowledge/{knowledge_id}", response_model=KnowledgeDocItem)
async def update_knowledge(
    knowledge_id: str,
    req: KnowledgeDocUpdateRequest,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeDocItem:
    """Rename a document, and/or replace its text and rebuild the vectors (异步)."""
    if req.title is None and req.content is None:
        raise HTTPException(status_code=400, detail="请至少提供 title 或 content")
    
    # 测试环境使用同步模式
    sync_mode = os.environ.get("APP_TEST_MODE", "false").lower() == "true"
    
    if sync_mode:
        # 测试中直接执行并返回结果
        result = await services.rag.update_knowledge(session, knowledge_id, req)
        if result is None:
            raise HTTPException(status_code=404, detail="知识文档不存在")
        return result
    else:
        # 生产环境异步入队
        queue = get_queue()
        task_id = await queue.enqueue(
            services.rag_queue.RAGTask(
                task_type=TaskType.UPDATE,
                payload={"knowledge_id": knowledge_id, **req.model_dump()},
                created_at=time.time(),
            )
        )
        # 立即返回接受状态
        return KnowledgeDocItem(
            knowledge_id=knowledge_id,
            title=req.title or "",
            provider=get_settings().llm_default_provider,
            chunks=0,
            chars=0,
            preview="",
            has_content=False,
            kind="text",
            updated_at=time.time(),
            task_id=task_id,
        )


@router.post("/knowledge/{knowledge_id}/reindex", response_model=KnowledgeReindexResponse)
async def reindex_knowledge(
    knowledge_id: str,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeReindexResponse:
    """Rebuild vectors with the currently configured provider (异步)."""
    # 测试环境使用同步模式
    sync_mode = os.environ.get("APP_TEST_MODE", "false").lower() == "true"
    
    if sync_mode:
        # 测试中直接执行并返回结果
        result = await services.rag.reindex_knowledge(session, knowledge_id)
        if result is None:
            raise HTTPException(status_code=404, detail="知识文档不存在")
        return result
    else:
        # 生产环境异步入队
        queue = get_queue()
        task_id = await queue.enqueue(
            services.rag_queue.RAGTask(
                task_type=services.rag_queue.TaskType.REINDEX,
                payload={"knowledge_id": knowledge_id},
                created_at=time.time(),
            )
        )
        # 立即返回接受状态
        return KnowledgeReindexResponse(
            knowledge_id=knowledge_id,
            provider=get_settings().llm_default_provider,
            previous_provider=None,
            chunks=0,
            chars=0,
            task_id=task_id,
        )


@router.post("/knowledge/{knowledge_id}/replace-vectors", response_model=KnowledgeReindexResponse)
async def replace_vectors(
    knowledge_id: str,
    req: KnowledgeReplaceVectorsRequest,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeReindexResponse:
    """更换向量模型后重建索引（异步）。"""
    new_provider = req.provider or get_settings().llm_default_provider
    
    queue = get_queue()
    task_id = await queue.enqueue(
        services.rag_queue.RAGTask(
            task_type=services.rag_queue.TaskType.REPLACE_VECTORS,
            payload={"knowledge_id": knowledge_id, "new_provider": new_provider},
            created_at=time.time(),
        )
    )
    # 立即返回接受状态
    return KnowledgeReindexResponse(
        knowledge_id=knowledge_id,
        provider=new_provider,
        previous_provider=None,
        chunks=0,
        chars=0,
        task_id=task_id,
    )


@router.delete("/knowledge/{knowledge_id}")
async def delete_knowledge(
    knowledge_id: str,
    session: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    if not await services.rag.delete_knowledge(session, knowledge_id):
        raise HTTPException(status_code=404, detail="知识文档不存在")
    return {"deleted": 1}


@router.post("/knowledge/search", response_model=KnowledgeSearchResponse)
async def search(
    req: KnowledgeSearchRequest,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeSearchResponse:
    return await services.rag.search(session, req)


@router.post("/knowledge/answer", response_model=KnowledgeAnswerResponse)
async def answer(
    req: KnowledgeAnswerRequest,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeAnswerResponse:
    """知识库问答（同步）。
    
    如需流式响应，设置 req.stream=true，将返回 StreamingResponse。
    """
    # 测试环境使用同步模式
    sync_mode = os.environ.get("APP_TEST_MODE", "false").lower() == "true"
    
    if getattr(req, "stream", False):
        return await _stream_answer(req, sync=sync_mode)
    
    # 非流式直接调用服务层
    result = await services.rag.answer(session, req)
    return result


async def _stream_answer(req: KnowledgeAnswerRequest, sync: bool = False) -> StreamingResponse:
    """知识库问答的流式生成器。"""
    session = await get_db().__anext__()
    
    async def gen():
        try:
            # 搜索相关片段
            search_resp = await services.rag.search(
                session,
                KnowledgeSearchRequest(
                    query=req.query, top_k=req.top_k, provider=req.provider
                ),
            )
            context = "\n".join(f"[{c.index}] {c.snippet}" for c in search_resp.items)
            question = f"Context:\n{context}\n\nQuestion: {req.query}"
            
            # Multimodal: when the caller attaches images (base64 data URL or http URL),
            # send them as content parts alongside the text so a vision model can reason
            # over them. Plain-text-only models ignore / reject these parts upstream.
            if req.images:
                user_content = [
                    {"type": "text", "text": question},
                    *(
                        {"type": "image_url", "image_url": {"url": url}}
                        for url in req.images
                        if url
                    ),
                ]
            else:
                user_content = question
            
            backend = llm.get_llm(req.provider)
            system_prompt = (
                "你是一个知识库问答助手。请基于下方提供的检索片段（Context）作答，"
                "并遵守以下要求：\n"
                "1. 先理解并整合这些片段，用你自己的话组织成条理清晰、结构化的回答，"
                "不要简单拼接或整段照抄原文；\n"
                "2. 内容较长时用 Markdown 组织：用小标题（### ）分段、用要点列表（- 或 1. ）罗列，"
                "突出关键结论；\n"
                "3. 引用了某个片段的结论时在句末标注来源编号 [n]（对应 Context 中的编号）；\n"
                "4. 只依据 Context 作答，Context 里没有的信息要明确说明「知识库中未找到相关内容」，不要编造。"
            )
            
            usage = None
            # 如果是同步模式，直接调用 chat 并流式输出
            if sync:
                content, _finish, usage, _model = await backend.chat(
                    [
                        ChatMessage(role="system", content=system_prompt),
                        ChatMessage(role="user", content=user_content),
                    ]
                )
                # 一次性发送完整回答
                yield json.dumps(KnowledgeAnswerChunk(
                    answer=content,
                    citations=search_resp.items,
                    usage=usage,
                ).model_dump(), ensure_ascii=False) + "\n"
            else:
                # 异步流式模式：逐块发送
                async for chunk in backend.chat_stream(
                    [
                        ChatMessage(role="system", content=system_prompt),
                        ChatMessage(role="user", content=user_content),
                    ]
                ):
                    yield json.dumps(KnowledgeAnswerChunk(
                        answer=getattr(chunk, 'content', ''),
                    ).model_dump(), ensure_ascii=False) + "\n"
                    usage = getattr(chunk, 'usage', None)
                
                # 最后一块发送引用和用量
                yield json.dumps(KnowledgeAnswerChunk(
                    answer="",
                    citations=search_resp.items,
                    usage=usage,
                ).model_dump(), ensure_ascii=False) + "\n"
        except Exception as exc:
            logger.exception("流式知识库问答中断")
            tail = KnowledgeAnswerChunk(error=f"生成中断：{exc}")
            yield json.dumps(tail.model_dump(), ensure_ascii=False) + "\n"
        finally:
            await session.close()
    
    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no"},
    )
