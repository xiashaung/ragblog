"""RAG 任务队列：文档上传、处理、重建、更换向量模型后的重建都放到队列执行，避免阻塞 HTTP 请求。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import KnowledgeDoc
from ..schemas.ai import (
    KnowledgeDocUpdateRequest,
    KnowledgeFetchRequest,
    KnowledgeIngestRequest,
    KnowledgePairsIngestRequest,
)
from . import rag

logger = logging.getLogger(__name__)


class TaskType(str, Enum):
    """RAG 任务类型。"""

    INGEST = "ingest"  # 文档入库
    INGEST_PAIRS = "ingest_pairs"  # 问答对入库
    INGEST_URL = "ingest_url"  # URL 抓取入库
    UPDATE = "update"  # 更新文档（含重建）
    REINDEX = "reindex"  # 重建索引
    REPLACE_VECTORS = "replace_vectors"  # 更换向量模型后重建


@dataclass
class RAGTask:
    """RAG 队列任务。"""

    task_type: TaskType
    payload: dict[str, Any]
    created_at: float


class RAGQueue:
    """RAG 任务队列（单 Worker）。

    所有耗时的 RAG 操作（分块、向量化、入库）都在后台队列中顺序执行，
    HTTP 接口只负责入队并立即返回“接受”状态。
    """

    def __init__(self):
        self._queue: asyncio.Queue[RAGTask | None] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """启动队列 Worker。"""
        if self._running:
            return
        self._running = True
        self._worker = asyncio.create_task(self._worker_loop())
        logger.info("RAG 队列 Worker 已启动")

    async def stop(self) -> None:
        """停止队列 Worker。"""
        self._running = False
        if self._queue is not None:
            await self._queue.put(None)  # 触发退出
        if self._worker is not None:
            await self._worker
        logger.info("RAG 队列 Worker 已停止")

    async def _worker_loop(self) -> None:
        """Worker 主循环：从队列取任务并顺序执行。"""
        while True:
            task = await self._queue.get()
            if task is None:
                break
            try:
                await self._execute_task(task)
            except Exception:  # noqa: BLE001 - 记录错误但不中断 worker
                logger.exception("RAG 任务执行失败：%s", task)

    async def _execute_task(self, task: RAGTask) -> None:
        """执行单个任务（在异步上下文中运行 DB 操作）。"""
        logger.info("开始执行 RAG 任务：%s", task.task_type.value)

        # 创建独立的数据库会话（与 HTTP 请求的会话隔离）
        from .. import db

        async with db.engine.connect() as conn:
            async with conn.begin():
                from sqlalchemy.ext.asyncio import async_sessionmaker

                session_maker = async_sessionmaker(
                    conn, expire_on_commit=False, class_=AsyncSession
                )
                async with session_maker() as session:
                    if task.task_type == TaskType.INGEST:
                        result = await self._do_ingest(session, task.payload)
                    elif task.task_type == TaskType.INGEST_PAIRS:
                        result = await self._do_ingest_pairs(session, task.payload)
                    elif task.task_type == TaskType.INGEST_URL:
                        result = await self._do_ingest_url(session, task.payload)
                    elif task.task_type == TaskType.UPDATE:
                        result = await self._do_update(session, task.payload)
                    elif task.task_type == TaskType.REINDEX:
                        result = await self._do_reindex(session, task.payload)
                    elif task.task_type == TaskType.REPLACE_VECTORS:
                        result = await self._do_replace_vectors(session, task.payload)
                    else:
                        raise ValueError(f"未知任务类型：{task.task_type}")

                    # 将结果写入任务元数据（可选）
                    doc_id = result.get("knowledge_id") if isinstance(result, dict) else None
                    if doc_id:
                        doc = await session.get(KnowledgeDoc, doc_id)
                        if doc and doc.meta is None:
                            doc.meta = {}
                        doc.meta["last_task_result"] = result
                        await session.flush()

        logger.info("RAG 任务执行完成：%s", task.task_type.value)

    async def _do_ingest(self, session: AsyncSession, payload: dict) -> dict:
        """执行文档入库。"""
        req = KnowledgeIngestRequest(**payload)
        result = await rag.ingest(session, req)
        return {
            "knowledge_id": result.knowledge_id,
            "provider": result.provider,
            "chunks": result.chunks,
            "chars": result.chars,
        }

    async def _do_ingest_pairs(self, session: AsyncSession, payload: dict) -> dict:
        """执行问答对入库。"""
        req = KnowledgePairsIngestRequest(**payload)
        result = await rag.ingest_pairs(session, req)
        return {
            "knowledge_id": result.knowledge_id,
            "provider": result.provider,
            "chunks": result.chunks,
            "chars": result.chars,
        }

    async def _do_ingest_url(self, session: AsyncSession, payload: dict) -> dict:
        """执行 URL 抓取入库。"""
        req = KnowledgeFetchRequest(**payload)
        result = await rag.ingest_from_url(session, req)
        return {
            "knowledge_id": result.knowledge_id,
            "provider": result.provider,
            "chunks": result.chunks,
            "chars": result.page_chars,
            "title": result.title,
            "url": result.url,
        }

    async def _do_update(self, session: AsyncSession, payload: dict) -> dict:
        """执行文档更新（含重建）。"""
        req = KnowledgeDocUpdateRequest(**payload)
        result = await rag.update_knowledge(session, payload["knowledge_id"], req)
        return {"updated": result is not None, "result": result}

    async def _do_reindex(self, session: AsyncSession, payload: dict) -> dict:
        """执行重建索引。"""
        result = await rag.reindex_knowledge(session, payload["knowledge_id"])
        if result is None:
            raise ValueError(f"文档不存在：{payload['knowledge_id']}")
        return result

    async def _do_replace_vectors(self, session: AsyncSession, payload: dict) -> dict:
        """执行更换向量模型后的重建。"""
        from ..services import llm

        doc_id = payload["knowledge_id"]
        new_provider = payload.get("new_provider")
        doc = await session.get(KnowledgeDoc, doc_id)
        if doc is None:
            raise ValueError(f"文档不存在：{doc_id}")
        if not doc.content:
            raise ValueError("该文档没有保存原文，无法重建索引，请重新上传文件")

        backend = llm.get_llm(new_provider)
        chunks = await rag.replace_vectors(
            session, doc, doc.content, backend=backend
        )
        return {
            "knowledge_id": doc.id,
            "provider": doc.provider,
            "previous_provider": doc.provider,
            "chunks": chunks,
            "chars": len(doc.content),
        }

    async def enqueue(self, task: RAGTask, sync: bool = False) -> str:
        """将任务加入队列，返回任务 ID（当前简化为时间戳字符串）。
        
        Args:
            task: 要执行的任务
            sync: 是否同步执行（用于测试或紧急场景）
        """
        task_id = f"rag-{int(task.created_at * 1000)}"
        task.id = task_id
        
        if sync:
            # 同步执行（用于测试）
            logger.info("同步执行 RAG 任务：%s (%s)", task.task_type.value, task_id)
            await self._execute_task_sync(task)
        else:
            # 异步入队
            await self._queue.put(task)
            logger.info("RAG 任务已入队：%s (%s)", task.task_type.value, task_id)
        
        return task_id
    
    async def _execute_task_sync(self, task: RAGTask) -> None:
        """同步执行任务（不启动 worker）。"""
        from .. import db
        from sqlalchemy.ext.asyncio import async_sessionmaker
        
        async with db.engine.connect() as conn:
            async with conn.begin():
                session_maker = async_sessionmaker(
                    conn, expire_on_commit=False, class_=AsyncSession
                )
                async with session_maker() as session:
                    if task.task_type == TaskType.INGEST:
                        result = await self._do_ingest(session, task.payload)
                    elif task.task_type == TaskType.INGEST_PAIRS:
                        result = await self._do_ingest_pairs(session, task.payload)
                    elif task.task_type == TaskType.INGEST_URL:
                        result = await self._do_ingest_url(session, task.payload)
                    elif task.task_type == TaskType.UPDATE:
                        result = await self._do_update(session, task.payload)
                    elif task.task_type == TaskType.REINDEX:
                        result = await self._do_reindex(session, task.payload)
                    elif task.task_type == TaskType.REPLACE_VECTORS:
                        result = await self._do_replace_vectors(session, task.payload)
                    else:
                        raise ValueError(f"未知任务类型：{task.task_type}")
                    logger.info("同步执行完成：%s", task.task_type.value)


# 全局队列实例
_queue: RAGQueue | None = None


def get_queue() -> RAGQueue:
    """获取全局 RAG 队列实例。"""
    global _queue
    if _queue is None:
        _queue = RAGQueue()
    return _queue


async def start_queue() -> None:
    """启动 RAG 队列（应用启动时调用）。"""
    q = get_queue()
    await q.start()


async def stop_queue() -> None:
    """停止 RAG 队列（应用关闭时调用）。"""
    q = get_queue()
    await q.stop()
