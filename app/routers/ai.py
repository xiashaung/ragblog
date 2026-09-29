"""AI 相关路由：对话与 RAG 知识库。

所有 AI 接口都接受一个可选的 ``provider`` 字段；没传时用「系统配置」里为该功能
选的 provider（``chat_provider`` / ``rag_provider`` / ``embedding_provider``），
只有库里什么都没配时才回退到 ``APP_LLM_DEFAULT_PROVIDER``。其中
``embedding_provider`` 只服务 RAG 的入库与检索（在 ``services.rag`` 里用），
本层不再暴露对外的文本转向量接口。
支持的取值：``openai`` / ``deepseek`` / ``qwen`` / ``ollama`` / ``mlx`` / ``mock``，
加上在「模型接入」里新建的任意自定义 provider。

本层只做「选后端 + 拼响应」：工具循环与流式清洗在 ``services.chat_tools``，
知识库的一整链路在 ``services.rag``，录入类重活则交给 ``services.rag_queue``。

鉴权口径：知识库相关接口（``/knowledge/*``，含上传路由）按 RBAC 拦截，
查资料要 ``knowledge:read``、改内容要 ``knowledge:manage``；
对话本体（``/chat`` 与它自己的会话）只要求后台登录态，不涉权限码。
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

from .. import audit, deps, services
from ..config import Provider, get_settings
from ..db import get_db
from ..rate_limit import admin_ai_limiter, enforce_sliding_limit
from ..rbac import Permission
from ..schemas.ai import (
    ChatAppendRequest,
    ChatAppendResponse,
    ChatChunk,
    ChatMessage,
    ChatMessageListResponse,
    ChatRequest,
    ChatResponse,
    ChatSessionCreate,
    ChatSessionListResponse,
    ChatSessionOut,
    ChatSessionUpdate,
    DocStatus,
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
    KnowledgeImageSearchRequest,
    KnowledgeImageSearchResponse,
    KnowledgeIngestRequest,
    KnowledgeIngestResponse,
    KnowledgeListResponse,
    KnowledgePairsIngestRequest,
    KnowledgeReindexAllResponse,
    KnowledgeReindexResponse,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeStatsResponse,
    RagTaskListResponse,
    RagTaskStatus,
    Usage,
    WebSource,
)
from ..services.llm import chat_model, default_chat_provider, get_llm
from ..services.provider_store import get_provider_config, known_providers
from ..services.rag_queue import TaskType, get_queue
from ..services.rbac_service import Principal

router = APIRouter()
logger = logging.getLogger(__name__)

def _enforce_admin_ai_limit(admin_id: str) -> None:
    """控制台昂贵 AI 端点的成本闸：按登录账号滑窗，超限 429 + Retry-After。

    与公开博客侧按 IP 不同，这里资源已需后台登录态，按 ``admin_id`` 计数能拿准
    「同一个人无论从哪调都算一份预算」；``/ai/chat``、``/ai/knowledge/answer``、
    ``/analytics/query`` 共用同一个 ``admin_ai_limiter`` 窗口。流式端点也在进流
    之前拦（生成器里抛异常已来不及改写响应头）。
    """
    settings = get_settings()
    enforce_sliding_limit(
        admin_ai_limiter,
        f"admin|{admin_id}",
        enabled=settings.admin_ai_rate_limit_enabled,
        window_seconds=settings.admin_ai_window_seconds,
        max_requests=settings.admin_ai_max_requests,
        message="AI 请求过于频繁",
    )


def _sync_mode() -> bool:
    """录入类接口是否在测试里同步执行。

    生产走后台队列（立即返回 task_id，实际处理异步做），但测试要拿到
    chunks / title 这些真实结果，所以 ``APP_TEST_MODE=true`` 时直接调用服务层，
    用请求自己的会话把数据落库。抛出的 ``ValueError`` 由全局处理器转成 400，
    中文原因原样透出。
    """
    return os.environ.get("APP_TEST_MODE", "false").lower() == "true"


@router.get("/providers", response_model=list[dict])
async def list_providers(
    _admin_id: str = Depends(deps.require_admin_session),
) -> list[dict]:
    """把各 provider 的默认值透出去，让前端预选下拉框、并在真 provider 没配
    API Key 时能提醒用户。

    内置项以 ``.env`` 为准，再合并「模型接入」里新建的自定义 provider（数据库优先）。

    这里故意返一个裸 JSON 数组：信封中间件只包对象，包成 ``{"data": [...]}``
    会让客户端拿到双层嵌套，所有 ``providers.filter(...)`` 写法全坏。
    """
    settings = get_settings()
    default = default_chat_provider()
    out = []
    seen: set[str] = set()
    for name in [p.value for p in Provider] + known_providers():
        if name in seen:
            continue
        seen.add(name)
        env_cfg = settings.provider_config(name) if name in {p.value for p in Provider} else None
        db_cfg = get_provider_config(name)
        cfg = db_cfg or env_cfg
        if cfg is None:
            continue
        out.append(
            {
                "name": name,
                "base_url": cfg.base_url,
                "chat_model": cfg.chat_model,
                "embedding_model": cfg.embedding_model,
                "image_embedding_model": cfg.image_embedding_model,
                "api_key_configured": bool(cfg.api_key)
                or name in ("ollama", "mlx", "mock"),
            }
        )
    out.sort(key=lambda p: (p["name"] != default, str(p["name"])))
    return out


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    _session: AsyncSession = Depends(get_db),
    admin_id: str = Depends(deps.require_admin_session),
) -> ChatResponse:
    """对话入口：``stream=true`` 走 NDJSON 流，否则一次返完整响应。

    ``provider=None`` 时由 ``services.llm.get_llm`` 回退到库里配的 chat_provider，
    实际用了哪个从后端对象读回来。两条路径共用 :func:`_turn_stream`，
    所以联网工具等非流式下也一样可用。
    """
    _enforce_admin_ai_limit(admin_id)
    llm = get_llm(req.provider)
    if req.stream:
        return await _stream_chat(req)
    # 非流式也走同一个工具循环，只是把块攒起来一次返回。刻意的：另写一条
    # ``llm.chat`` 直达路径会让「联网」只在流式可用，同一个接口两种行为比难查。
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
        model=req.model or chat_model(llm.provider),
        content="".join(parts),
        reasoning="".join(think),
        finish_reason=finish,
        usage=usage,
        web_sources=sources,
    )


def _turn_stream(llm: object, req: ChatRequest):
    """统一的「一轮问答」生成器（含工具循环），流式与非流式共用。"""
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
    """流式对话：每块一个 JSON 行（NDJSON），并带上 ``X-Accel-Buffering: no`` 防 nginx 攒块。"""
    llm = get_llm(req.provider)

    async def gen():
        """逐行边生边发（旧的 await 成 list 写法会把流式退化成「等全部生成完」）。"""
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
# 跨身份的会话 id 一律 404（服务层已把「不存在」与「不是你的」合并）。


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


# --- RAG / 知识库 ---
#
# 声明顺序有要求：静态路径（``/knowledge/stats``、``/knowledge/ingest*``、
# ``/knowledge/fetch-url``）必须排在 ``/knowledge/{knowledge_id}`` 之前，
# 否则路径参数会把它们吞掉。
#
# 权限划分：列表 / 详情 / 检索 / 问答算「读」（``knowledge:read``），
# 其余会改变库内容的（录入、编辑、重建、删除）一律算「写」（``knowledge:manage``）。


@router.post("/knowledge/ingest", response_model=KnowledgeIngestResponse)
async def ingest(
    req: KnowledgeIngestRequest,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_MANAGE)),
) -> KnowledgeIngestResponse:
    """文档入库：生产异步入队，测试同步执行（见 ``_sync_mode``）。

    正文格式由 ``req.format`` 声明（text / markdown / html），html 在服务层
    先转成 Markdown；入队时格式随 ``model_dump()`` 一起进 payload，两路行为一致。
    """
    if _sync_mode():
        # 测试中直接执行并返回结果
        result = await services.rag.ingest(session, req)
        return KnowledgeIngestResponse(
            knowledge_id=result.knowledge_id,
            status=result.status,
            chunks=result.chunks,
            chars=result.chars,
            task_id=None,
        )
    else:
        # 生产环境异步入队（任务行随请求会话落库，见 rag_queue.enqueue）
        queue = get_queue()
        task_id = await queue.enqueue(
            session,
            services.rag_queue.RAGTask(
                task_type=TaskType.INGEST,
                payload=req.model_dump(),
                created_at=time.time(),
            ),
            sync=False,
        )
        return KnowledgeIngestResponse(
            knowledge_id=f"pending-{task_id}",
            status=DocStatus.PENDING,
            chunks=0,
            chars=0,
            task_id=task_id,
        )


@router.post("/knowledge/ingest-pairs", response_model=KnowledgeIngestResponse)
async def ingest_pairs(
    req: KnowledgePairsIngestRequest,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_MANAGE)),
) -> KnowledgeIngestResponse:
    """录入一批问答对（FAQ），合为一篇文档重建索引。"""
    if _sync_mode():
        # 问答对全为空时服务层抛 ValueError → 400，必须在入队前发生
        return await services.rag.ingest_pairs(session, req)

    queue = get_queue()
    task_id = await queue.enqueue(
        session,
        services.rag_queue.RAGTask(
            task_type=TaskType.INGEST_PAIRS,
            payload=req.model_dump(),
            created_at=time.time(),
        ),
    )
    return KnowledgeIngestResponse(
        knowledge_id=f"pending-{task_id}",
        status=DocStatus.PENDING,
        chunks=0,
        chars=0,
        task_id=task_id,
    )


@router.post("/knowledge/fetch-url", response_model=KnowledgeFetchResponse)
async def ingest_from_url(
    req: KnowledgeFetchRequest,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_MANAGE)),
) -> KnowledgeFetchResponse:
    """抓取一个网页地址，抽正文后入库。"""
    if _sync_mode():
        # 抓取失败（含 HTTP 404）时服务层抛 ValueError → 400，带中文原因
        return await services.rag.ingest_from_url(session, req)

    queue = get_queue()
    task_id = await queue.enqueue(
        session,
        services.rag_queue.RAGTask(
            task_type=TaskType.INGEST_URL,
            payload=req.model_dump(),
            created_at=time.time(),
        ),
    )
    # 立即返回接受状态
    return KnowledgeFetchResponse(
        knowledge_id=f"pending-{task_id}",
        status=DocStatus.PENDING,
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
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_READ)),
) -> KnowledgeStatsResponse:
    """文档数 / 分块数总量，另带按向量化状态的拆解（看多少文档待重建 / 处理中 / 失败）。"""
    return await services.rag.knowledge_stats(session)


# 任务状态查询（异步录入后轮询）。静态段 ``/knowledge/tasks`` 必须排在
# ``/knowledge/{knowledge_id}`` 之前，否则会被路径参数吞掉（见文件顶部注释）。
@router.get("/knowledge/tasks", response_model=RagTaskListResponse)
async def list_rag_tasks(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_READ)),
) -> RagTaskListResponse:
    """分页列最近的 RAG 后台任务（按创建时间倒序）。"""
    total, rows = await services.rag_queue.list_tasks(
        session, offset=offset, limit=limit
    )
    return RagTaskListResponse(
        total=total,
        offset=offset,
        limit=limit,
        items=[RagTaskStatus(**services.rag_queue.to_status(r)) for r in rows],
    )


@router.get("/knowledge/tasks/{task_id}", response_model=RagTaskStatus)
async def get_rag_task(
    task_id: str,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_READ)),
) -> RagTaskStatus:
    """按 task_id 查一个 RAG 后台任务的状态（pending/running/success/failed）。"""
    row = await services.rag_queue.get_task(session, task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return RagTaskStatus(**services.rag_queue.to_status(row))


@router.get("/knowledge", response_model=KnowledgeListResponse)
async def list_knowledge(
    keyword: str | None = Query(default=None, description="按标题 / 正文模糊匹配"),
    status: str | None = Query(
        default=None,
        description="按向量化状态过滤：indexed/pending/processing/failed",
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_READ)),
) -> KnowledgeListResponse:
    """分页列出知识文档，支持标题/正文关键字与向量化状态过滤。"""
    return await services.rag.list_knowledge(
        session,
        keyword=keyword,
        status=status,
        offset=offset,
        limit=limit,
    )


@router.post("/knowledge/batch-delete", response_model=KnowledgeBatchDeleteResponse)
async def batch_delete_knowledge(
    req: KnowledgeBatchDeleteRequest,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_MANAGE)),
) -> KnowledgeBatchDeleteResponse:
    """批量删文档：逐条删，失败的单独回到 ``failed`` 里，不全盘回滚。"""
    deleted, failed = await services.rag.delete_knowledge_many(session, req.ids)
    return KnowledgeBatchDeleteResponse(deleted=deleted, failed=failed)


@router.get("/knowledge/{knowledge_id}", response_model=KnowledgeDetailResponse)
async def knowledge_detail(
    knowledge_id: str,
    chunk_limit: int = Query(default=200, ge=1, le=2000),
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_READ)),
) -> KnowledgeDetailResponse:
    """单篇文档详情 + 它的全部分块（最多 ``chunk_limit`` 块）。"""
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
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_MANAGE)),
) -> KnowledgeDocItem:
    """改标题，或换正文（连带重建向量），或两者都改。

    改了正文就得重算嵌入，所以同样走「测试同步 / 生产异步入队」两分支；
    入队时只能回一个占位对象 + ``task_id``。``format`` 随 ``model_dump()``
    一起进队 payload，由消费端重建请求模型后在 :func:`rag.update_knowledge` 里归一化。
    """
    if req.title is None and req.content is None:
        raise HTTPException(status_code=400, detail="请至少提供 title 或 content")

    if _sync_mode():
        # 测试中直接执行并返回结果
        result = await services.rag.update_knowledge(session, knowledge_id, req)
        if result is None:
            raise HTTPException(status_code=404, detail="知识文档不存在")
        return result
    else:
        # 生产环境异步入队
        queue = get_queue()
        task_id = await queue.enqueue(
            session,
            services.rag_queue.RAGTask(
                task_type=TaskType.UPDATE,
                payload={"knowledge_id": knowledge_id, **req.model_dump()},
                created_at=time.time(),
            ),
        )
        # 立即返回接受状态：正文将重算，占位对象先标「待重建」
        return KnowledgeDocItem(
            knowledge_id=knowledge_id,
            title=req.title or "",
            status=DocStatus.PENDING,
            chunks=0,
            chars=0,
            preview="",
            has_content=False,
            kind="text",
            # 占位对象也要把格式说对：html 存下来就是 markdown
            format=services.markdown_text.stored_format(req.format or "text"),
            updated_at=time.time(),
            task_id=task_id,
        )


@router.post("/knowledge/{knowledge_id}/reindex", response_model=KnowledgeReindexResponse)
async def reindex_knowledge(
    knowledge_id: str,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_MANAGE)),
) -> KnowledgeReindexResponse:
    """用当前配置的 provider 重建向量（换嵌入模型后把旧文档拉回一致）。"""
    if _sync_mode():
        # 测试中直接执行并返回结果
        result = await services.rag.reindex_knowledge(session, knowledge_id)
        if result is None:
            raise HTTPException(status_code=404, detail="知识文档不存在")
        return result
    else:
        # 生产环境异步入队
        queue = get_queue()
        task_id = await queue.enqueue(
            session,
            services.rag_queue.RAGTask(
                task_type=services.rag_queue.TaskType.REINDEX,
                payload={"knowledge_id": knowledge_id},
                created_at=time.time(),
            ),
        )
        # 立即返回接受状态：worker 将拉回加重建，占位先标「处理中」
        return KnowledgeReindexResponse(
            knowledge_id=knowledge_id,
            status=DocStatus.PROCESSING,
            chunks=0,
            chars=0,
            task_id=task_id,
        )


@router.post("/knowledge/reindex-all", response_model=KnowledgeReindexAllResponse)
async def reindex_all_knowledge(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_MANAGE)),
) -> KnowledgeReindexAllResponse:
    """全库强制重建：把所有文档标为待重建并入队 REINDEX_ALL。

    有原文的文档置 ``pending``，无原文的老文档置 ``failed``（需重新上传）；
    先提交状态变更再入队，保证 worker 新会话能读到。同步模式下直接跑完全库重建。
    """
    enqueued = await services.rag.mark_docs_for_full_rebuild(session)
    await session.commit()
    if _sync_mode():
        await services.rag.reindex_all_docs(session)
        return KnowledgeReindexAllResponse(enqueued=enqueued, task_id=None)
    queue = get_queue()
    task_id = await queue.enqueue(
        session,
        services.rag_queue.RAGTask(
            task_type=TaskType.REINDEX_ALL,
            payload={},
            created_at=time.time(),
        ),
    )
    return KnowledgeReindexAllResponse(enqueued=enqueued, task_id=task_id)


@router.delete("/knowledge/{knowledge_id}")
async def delete_knowledge(
    knowledge_id: str,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_MANAGE)),
) -> dict[str, int]:
    """删一篇文档（连带它的分块）；id 不存在返 404。"""
    if not await services.rag.delete_knowledge(session, knowledge_id):
        raise HTTPException(status_code=404, detail="知识文档不存在")
    audit.event(audit.Actor.from_principal(principal), "knowledge.delete", knowledge_id)
    return {"deleted": 1}


@router.post("/knowledge/search", response_model=KnowledgeSearchResponse)
async def search(
    req: KnowledgeSearchRequest,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_READ)),
) -> KnowledgeSearchResponse:
    """向量/混合检索：参数开关（融合、精排）都在请求体里，流程见 ``rag.search``。"""
    return await services.rag.search(session, req)


@router.post("/knowledge/search-images", response_model=KnowledgeImageSearchResponse)
async def search_images(
    req: KnowledgeImageSearchRequest,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_READ)),
) -> KnowledgeImageSearchResponse:
    """文搜图：拿一段文字去召回最相近的图片（跨模态），流程见 ``rag.search_images``。

    只命「已学习」（indexed）图片文档；实际用的 VL provider 从后端回读，方便
    前端展示“这张图是用哪个模型搜出来的”。
    """
    hits = await services.rag.search_images(
        session, req.query, req.top_k, provider=req.provider
    )
    provider = get_llm(req.provider, feature="image_embedding").provider
    return KnowledgeImageSearchResponse(query=req.query, provider=provider, items=hits)


@router.post("/knowledge/answer", response_model=KnowledgeAnswerResponse)
async def answer(
    req: KnowledgeAnswerRequest,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_READ)),
) -> KnowledgeAnswerResponse:
    """知识库问答（同步）。
    
    如需流式响应，设置 req.stream=true，将返回 StreamingResponse。
    """
    # 流式与非流式共用的成本闸：放在分叉之前，一次覆盖两条路径。
    _enforce_admin_ai_limit(principal.admin_id)
    # 测试环境使用同步模式
    sync_mode = _sync_mode()
    
    if getattr(req, "stream", False):
        return await _stream_answer(req, sync=sync_mode)
    
    # 非流式直接调用服务层
    result = await services.rag.answer(session, req)
    return result


async def _stream_answer(req: KnowledgeAnswerRequest, sync: bool = False) -> StreamingResponse:
    """知识库问答的流式生成器。

    自己另开一个会话而不是用 ``Depends(get_db)``：生成器活得比请求级会话长，
    请求级会话在响应交出去时就该被释放，再用它就晚了，所以在 ``finally`` 里自己关。
    """
    session = await get_db().__anext__()

    async def gen():
        """先检索拼上下文，再让模型作答；末尾补一块带引用与 usage 的收尾。"""
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
            
            # 多模态：调用方附了图（base64 data URL 或 http URL）时，把图与文字
            # 一起作为 content parts 发出，让支持视觉的模型能看着图回答。
            # 纯文本模型会由上游忽略 / 拒收这些 part。
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
            
            backend = get_llm(req.provider, feature="rag")
            system_prompt = (
                "你是一个知识库问答助手。请基于下方提供的检索片段（Context）作答，"
                "并遵守以下要求：\n"
                "1. 先理解并整合这些片段，用你自己的话组织成条理清晰、结构化的回答，"
                "不要简单拼接或整段照抄原文；\n"
                "2. 内容较长时用 Markdown 组织：用小标题（### ）分段、用要点列表（- 或 1. ）罗列，"
                "突出关键结论；\n"
                "3. 引用了某个片段的结论时在句末标注来源编号 [n]（对应 Context 中的编号）；\n"
                "4. 只依据 Context 作答，Context 里没有的信息要明确说明"
                "「知识库中未找到相关内容」，不要编造。"
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
                final_usage = None
                async for chunk in backend.chat_stream(
                    [
                        ChatMessage(role="system", content=system_prompt),
                        ChatMessage(role="user", content=user_content),
                    ]
                ):
                    # 收集内容块和引用信息（done 标志落在最后一块，引用随它一起发）
                    answer_chunk = KnowledgeAnswerChunk(
                        answer=getattr(chunk, 'content', ''),
                        citations=search_resp.items if getattr(chunk, 'done', False) else [],
                        usage=None,
                    )
                    yield json.dumps(answer_chunk.model_dump(), ensure_ascii=False) + "\n"
                    
                    # 保存最后一块的 usage
                    if hasattr(chunk, 'usage') and chunk.usage:
                        final_usage = chunk.usage
                
                # 最后一块发送引用和用量
                if final_usage or search_resp.items:
                    yield json.dumps(KnowledgeAnswerChunk(
                        answer="",
                        citations=search_resp.items,
                        usage=final_usage,
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
