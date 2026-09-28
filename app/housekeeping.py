"""后台巡检：给会无限增长的两张表做保留清理。

- ``rag_tasks``：每次 RAG 写入操作都落一行（含 payload 快照），终态
  （success/failed）行只在状态端点查历史时才用到——超过保留天数的直接删。
  pending/running 一律不动（那是还在跑或待跑的）。
- ``admin_sessions``：token 过期后行不会被立刻清（``verify_token`` 只在被
  再次命中时才顺手删自己那条），长期攒下大量死行——按 ``expires_at`` 清理。

运行时机：应用启动跑一次，之后每隔 ``housekeeping_interval_seconds`` 跑一次
（见 main.py lifespan 挂的这个协程）。单实例内的周期任务，多 Worker 各自跑
各自的最坏结果也只是重复删同一批已删的行（幂等 DELETE），无副作用。
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import delete, or_

from . import db
from .config import get_settings
from .models import AdminSession, RagTaskRow
from .services.rag_queue import FAILED, SUCCESS

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = (SUCCESS, FAILED)


async def purge_expired_sessions(session) -> int:
    """删已过期（``expires_at`` 早于现在）的会话行，返删除数。"""
    now = time.time()
    result = await session.execute(
        delete(AdminSession).where(AdminSession.expires_at < now)
    )
    return int(result.rowcount or 0)


async def purge_old_tasks(session, *, keep_days: int) -> int:
    """删终态且已超过保留天数的任务行；``finished_at`` 缺失时退回 ``created_at`` 判。"""
    cutoff = time.time() - keep_days * 86_400
    # 终态且（完成时间或创建时间）够老；pending/running 永不命中状态条件
    stale = or_(
        RagTaskRow.finished_at < cutoff,
        RagTaskRow.finished_at.is_(None) & (RagTaskRow.created_at < cutoff),
    )
    result = await session.execute(
        delete(RagTaskRow).where(RagTaskRow.status.in_(_TERMINAL_STATUSES), stale)
    )
    return int(result.rowcount or 0)


async def run_once() -> dict[str, int]:
    """开一个短会话跑两样清理，提交后返各自删除数。"""
    settings = get_settings()
    session_factory = db.get_session_factory()
    async with session_factory() as session:
        sessions_purged = await purge_expired_sessions(session)
        tasks_purged = await purge_old_tasks(
            session, keep_days=settings.rag_task_retention_days
        )
        await session.commit()
    stats = {"sessions_purged": sessions_purged, "tasks_purged": tasks_purged}
    if sessions_purged or tasks_purged:
        logger.info("后台巡检清理完成：%s", stats)
    return stats


async def housekeeping_loop() -> None:
    """周期巡检协程主体：先睡满 interval 再跑，如此反复。

    启动时的首次清理由 :func:`start` 在服务开始前同步做完，本循环只负责
    后续周期——故这里「先睡后跑」，避免开跑就与实时请求争写连接。
    取消（应用关闭）时干净退出；单轮异常只记日志、不终止整个循环——
    一次清理失败不该让保留策略从此再也不跑。
    """
    settings = get_settings()
    interval = max(60, settings.housekeeping_interval_seconds)
    logger.info(
        "后台巡检周期任务已启动（每 %d 秒，任务保留 %d 天）",
        interval,
        settings.rag_task_retention_days,
    )
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await run_once()
            except Exception:  # noqa: BLE001 - 单轮失败不能停掉整个巡检
                logger.exception("后台巡检执行失败，将在下一周期重试")
    except asyncio.CancelledError:
        logger.info("后台巡检已停止")
        raise


async def start() -> asyncio.Task | None:
    """启动时同步跑一次清理（服务开始前完成），再挂周期任务；关开关返 None。

    首次清理不能交给后台任务「开跑就执行」——那样会与刚进来的实时请求
    并发写同一库（SQLite 单写者），制造无谓的锁争用。这里在 lifespan
    里 `await` 完，服务还没开始接单，不与其他写操作抢连接。
    """
    if not get_settings().housekeeping_enabled:
        logger.info("后台巡检已关闭（APP_HOUSEKEEPING_ENABLED=false）")
        return None
    await run_once()  # 启动清理：同步完成，不与实时流量并发
    return asyncio.create_task(housekeeping_loop())


__all__ = [
    "housekeeping_loop",
    "purge_expired_sessions",
    "purge_old_tasks",
    "run_once",
    "start",
]
