"""RAG 任务队列：文档上传、处理、重建、更换向量模型后的重建都放到队列执行，避免阻塞 HTTP 请求。

任务生命周期（行由 ``rag_tasks`` 表持久化，见 models.RagTaskRow）：

1. 路由把 payload 交给 :meth:`RAGQueue.enqueue`，用**请求自己的会话**写入
   ``pending`` 行并随请求 commit——任务登记与进程无关，重启后仍能查到历史；
2. ``await queue.put(task_id)`` 把 id 投进内存队列，HTTP 立即返回 ``task_id``；
3. 多个 Worker 并发从队列取 id，各自开会话把行推进到 ``running`` →
   ``success`` / ``failed``，结果与失败原因都写回同一行。

内存队列丢了（进程重启）不会丢任务登记：存量 ``pending`` 会在启动日志里告警，
但不自动重跑——半截任务（如已删旧分片未写新分片）重复执行比丢任务更糟，
交由管理员在后台确认后重新发起。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db
from ..config import get_settings
from ..metrics import rag_tasks_finished_total
from ..models import KnowledgeDoc, RagTaskRow
from ..schemas.ai import (
    KnowledgeDocUpdateRequest,
    KnowledgeFetchRequest,
    KnowledgeIngestRequest,
    KnowledgePairsIngestRequest,
)
from . import rag

logger = logging.getLogger(__name__)

# 状态机：与 RagTaskRow.status 的取值一致
PENDING = "pending"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"


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
    """RAG 队列任务（内存投递形态；持久化形态见 RagTaskRow）。"""

    task_type: TaskType
    payload: dict[str, Any]
    created_at: float
    id: str = ""


class RAGQueue:
    """RAG 任务队列（多 Worker 并发）。

    所有耗时的 RAG 操作（分块、向量化、入库）都在后台队列执行，
    HTTP 接口只负责登记任务并入队并立即返回「接受」状态。并发度由
    ``APP_RAG_QUEUE_WORKERS`` 控制（默认 2）：单 Worker 时一个大文档会堵住
    整条队列，多 Worker 让不同文档并行处理；同一文档的任务仍按入队顺序
    串行（Worker 取到即先置 running，重复投递会被状态检查跳过）。
    """

    def __init__(self) -> None:
        """只建空队列与占位字段，起 Worker 要显式调 :meth:`start`。"""
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._running = False

    @property
    def workers_alive(self) -> int:
        """当前存活的 Worker 数（健康检查用；0 表示队列停摆）。"""
        return sum(1 for w in self._workers if not w.done())

    async def start(self) -> None:
        """启动全部 Worker，并对重启遗留的 pending 任务告警。"""
        if self._running:
            return
        self._running = True
        # 每次启动重建 asyncio 队列对象：Queue 绑定首次使用的事件循环，
        # 旧实例拿到新循环里用会直接 RuntimeError（测试每个用例新建循环时尤其易现）
        self._queue = asyncio.Queue()
        n = max(1, get_settings().rag_queue_workers)
        self._workers = [
            asyncio.create_task(self._worker_loop(i)) for i in range(n)
        ]
        logger.info("RAG 队列 Worker 已启动（并发 %d）", n)
        await self._warn_orphans()

    async def _warn_orphans(self) -> None:
        """登记仍在 pending 的遗留任务（进程重启丢队列），提示人工处理。"""
        try:
            async with db.get_session_factory()() as session:
                rows = (
                    await session.scalars(
                        select(RagTaskRow).where(RagTaskRow.status == PENDING)
                    )
                ).all()
            if rows:
                logger.warning(
                    "发现 %d 个未执行的遗留 RAG 任务（进程重启丢队列），"
                    "请在后台确认后重新发起对应操作",
                    len(rows),
                )
        except Exception:  # noqa: BLE001 - 告警失败不影响启动
            logger.exception("检查遗留 RAG 任务失败")

    async def stop(self) -> None:
        """停止队列：按 Worker 数投退出哨兵，等全部收尾。"""
        self._running = False
        for _ in self._workers:
            await self._queue.put(None)
        for w in self._workers:
            await w
        self._workers = []
        logger.info("RAG 队列 Worker 已停止")

    async def _worker_loop(self, index: int) -> None:
        """单个 Worker 主循环：从队列取任务 id 并执行。"""
        while True:
            task_id = await self._queue.get()
            if task_id is None:
                break
            try:
                await self._run_task(task_id)
            except Exception:  # noqa: BLE001 - 记录错误但不中断 worker
                logger.exception("RAG 任务执行失败：%s", task_id)

    async def _run_task(self, task_id: str) -> None:
        """按 id 执行一个已登记的任务：置 running → 分发 → 写回结果/错误。

        状态流转各用独立短会话并立即提交：Worker 崩溃或任务失败时，
        「置为 running / failed」这两笔本身不能跟着任务内容一起回滚，
        否则行会永远卡在中间态。
        """
        payload, task_type = await self._mark_running(task_id)
        if payload is None:
            # 行不存在或已不在 pending（重复投递 / 遗留任务），跳过
            logger.warning("RAG 任务 %s 不在 pending 状态，跳过执行", task_id)
            return

        session_factory = db.get_session_factory()
        try:
            async with session_factory() as session:
                result = await self._dispatch(session, task_type, payload)
                await session.commit()
            await self._finish(task_id, status_=SUCCESS, result=result, error=None)
            logger.info("RAG 任务执行完成：%s (%s)", task_type, task_id)
        except Exception as exc:  # noqa: BLE001 - 失败要落库，不能只留日志
            async with session_factory() as session:
                await session.rollback()
            await self._finish(
                task_id, status_=FAILED, result=None, error=f"{type(exc).__name__}: {exc}"
            )
            logger.exception("RAG 任务执行失败：%s (%s)", task_type, task_id)

    async def _mark_running(self, task_id: str) -> tuple[dict | None, str]:
        """条件更新 pending→running；抢不到（状态不对）返回 ``None`` 让上层跳过。"""
        async with db.get_session_factory()() as session:
            row = await session.get(RagTaskRow, task_id)
            if row is None or row.status != PENDING:
                return None, ""
            row.status = RUNNING
            row.started_at = time.time()
            await session.commit()
            return dict(row.payload or {}), row.task_type

    async def _finish(
        self,
        task_id: str,
        *,
        status_: str,
        result: dict | None,
        error: str | None,
    ) -> None:
        """把终态（success/failed）与结果/错误写回任务行。"""
        rag_tasks_finished_total.inc((status_,))
        async with db.get_session_factory()() as session:
            row = await session.get(RagTaskRow, task_id)
            if row is None:
                return
            row.status = status_
            row.result = result
            row.error = error
            row.finished_at = time.time()
            if result and result.get("knowledge_id"):
                row.knowledge_id = str(result["knowledge_id"])
            await session.commit()

    async def _dispatch(
        self, session: AsyncSession, task_type: str, payload: dict
    ) -> dict:
        """按类型分发到具体执行函数（同步/异步两条路共用，避免重复 if/elif）。"""
        handlers: dict[
            str, Callable[[AsyncSession, dict], Coroutine[Any, Any, dict]]
        ] = {
            TaskType.INGEST.value: self._do_ingest,
            TaskType.INGEST_PAIRS.value: self._do_ingest_pairs,
            TaskType.INGEST_URL.value: self._do_ingest_url,
            TaskType.UPDATE.value: self._do_update,
            TaskType.REINDEX.value: self._do_reindex,
            TaskType.REPLACE_VECTORS.value: self._do_replace_vectors,
        }
        handler = handlers.get(task_type)
        if handler is None:
            raise ValueError(f"未知任务类型：{task_type}")
        return await handler(session, payload)

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

        # 没指定则以系统配置里的 embedding_provider 为准（而不是 .env 全局默认）
        backend = llm.get_llm(new_provider, feature="embedding")
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

    async def enqueue(
        self,
        session: AsyncSession,
        task: RAGTask,
        sync: bool = False,
    ) -> str:
        """登记任务（写 ``rag_tasks`` 行）并入队，返回任务 id。

        - ``session`` 用请求自己的会话：行随请求成功 commit，与队列解耦；
        - id 由 :func:`new_task` 当场生成（uuid），并写回 ``task.id``，
          路由层拿它拼 ``pending-{task_id}`` 响应；
        - ``sync=True``（测试）：不走 Worker，直接执行并写回终态，
          但任务行同样落库，让状态端点也测得到。
        """
        task.id = await self._create_row(session, task)
        if sync:
            logger.info("同步执行 RAG 任务：%s (%s)", task.task_type.value, task.id)
            # 先提交任务行：_run_task 用独立会话读它，未提交对其他连接不可见（SQLite 尤甚）
            await session.commit()
            await self._run_task(task.id)
        else:
            await self._queue.put(task.id)
            logger.info("RAG 任务已入队：%s (%s)", task.task_type.value, task.id)
        return task.id

    async def _create_row(self, session: AsyncSession, task: RAGTask) -> str:
        """写入 pending 行（payload 里的 model_dump 都可 JSON 序列化）。"""
        row = RagTaskRow(
            task_type=task.task_type.value,
            payload=task.payload,
            status=PENDING,
            created_at=task.created_at,
        )
        session.add(row)
        await session.flush()
        return row.id


async def get_task(session: AsyncSession, task_id: str) -> RagTaskRow | None:
    """按 id 查任务登记行（状态端点用）。"""
    return await session.get(RagTaskRow, task_id)


def to_status(row: RagTaskRow) -> dict[str, Any]:
    """RagTaskRow → 对外状态字典（与 schemas.ai.RagTaskStatus 字段对齐）。"""
    return {
        "task_id": row.id,
        "task_type": row.task_type,
        "status": row.status,
        "knowledge_id": row.knowledge_id,
        "result": row.result,
        "error": row.error,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


async def list_tasks(
    session: AsyncSession, *, offset: int = 0, limit: int = 20
) -> tuple[int, list[RagTaskRow]]:
    """分页列最近任务（按创建时间倒序）；回总数与当页行。"""
    from sqlalchemy import func

    total = await session.scalar(select(func.count()).select_from(RagTaskRow)) or 0
    rows = (
        await session.scalars(
            select(RagTaskRow)
            .order_by(RagTaskRow.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return int(total), list(rows)


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
