"""后台巡检的保留清理测试：终态任务行按保留天删、过期会话删，非终态不动。

直接对 purge_* 函数喂构造的行并断言删除计数——这两个函数是纯 SQL DELETE，
用真实 session 建/查最贴合，不必绕 HTTP。
"""

from __future__ import annotations

import time

from sqlalchemy import select

from app import db, housekeeping
from app.models import AdminSession, RagTaskRow
from app.services.admin_service import _hash_token
from app.services.rag_queue import FAILED, PENDING, RUNNING, SUCCESS


async def _fresh_session():
    return db.get_session_factory()()


async def _remaining_task_ids(session) -> set[str]:
    rows = (await session.scalars(select(RagTaskRow.id))).all()
    return set(rows)


async def _remaining_session_ids(session) -> set[str]:
    rows = (await session.scalars(select(AdminSession.id))).all()
    return set(rows)


async def test_purge_old_tasks_removes_only_stale_terminal(client) -> None:
    now = time.time()
    old = now - 31 * 86_400  # 31 天前，超过 30 天保留线
    async with await _fresh_session() as session:
        session.add_all(
            [
                RagTaskRow(task_type="ingest", status=SUCCESS, created_at=old, finished_at=old),
                RagTaskRow(task_type="ingest", status=FAILED, created_at=old, finished_at=old),
                # 终态但够新：留
                RagTaskRow(task_type="ingest", status=SUCCESS, created_at=now, finished_at=now),
                # 非终态：无论多旧都不动
                RagTaskRow(task_type="ingest", status=PENDING, created_at=old),
                RagTaskRow(task_type="ingest", status=RUNNING, created_at=old),
            ]
        )
        await session.commit()

    async with await _fresh_session() as session:
        purged = await housekeeping.purge_old_tasks(session, keep_days=30)
        await session.commit()
        assert purged == 2
        remaining = await _remaining_task_ids(session)
    assert len(remaining) == 3


async def test_purge_old_tasks_falls_back_to_created_at(client) -> None:
    """终态行没有 finished_at（历史脏数据）时，用 created_at 判够不够老。"""
    now = time.time()
    old = now - 40 * 86_400
    async with await _fresh_session() as session:
        session.add(
            RagTaskRow(
                task_type="ingest", status=SUCCESS, created_at=old, finished_at=None
            )
        )
        await session.commit()
    async with await _fresh_session() as session:
        purged = await housekeeping.purge_old_tasks(session, keep_days=30)
        await session.commit()
        assert purged == 1


async def test_purge_expired_sessions(client) -> None:
    now = time.time()
    async with await _fresh_session() as session:
        session.add_all(
            [
                AdminSession(
                    token_hash=_hash_token("t1"), admin_id="adm_x", expires_at=now - 10
                ),
                AdminSession(
                    token_hash=_hash_token("t2"), admin_id="adm_x", expires_at=now + 3600
                ),
            ]
        )
        await session.commit()
    async with await _fresh_session() as session:
        purged = await housekeeping.purge_expired_sessions(session)
        await session.commit()
        assert purged == 1
        remaining = await _remaining_session_ids(session)
    assert len(remaining) == 1


async def test_run_once_integrated(client) -> None:
    """run_once 走真实 get_session_factory，两样一起清并返回计数。"""
    now = time.time()
    old = now - 31 * 86_400
    async with await _fresh_session() as session:
        session.add_all(
            [
                RagTaskRow(task_type="ingest", status=SUCCESS, created_at=old, finished_at=old),
                AdminSession(token_hash=_hash_token("dead"), admin_id="adm_x", expires_at=now - 1),
            ]
        )
        await session.commit()

    stats = await housekeeping.run_once()
    assert stats == {"sessions_purged": 1, "tasks_purged": 1}
