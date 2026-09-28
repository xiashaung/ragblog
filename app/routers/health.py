"""健康检查路由：给探针 / 负载均衡用，由 ``create_app`` 挂在 ``/api/v1`` 下。

三个接口分工不同：

- ``/health`` 与 ``/health/live`` 只证明「进程还活着」，不碰任何依赖，
  适合存活探针（数据库抖一下不该导致进程被判定死亡、陷入重启循环）；
- ``/health/ready`` 真查两项依赖——主库 ``SELECT 1`` 与 RAG 队列 Worker——
  任一坏了返 503，负载均衡据此暂停切流量。Worker 少而不全只降级
  （``degraded``），仍能服务，不算失败。
"""

import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .. import db
from ..config import get_settings
from ..services import rag_queue

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """通用健康检查：能响应就返 ok（不探测依赖，语义同 live）。"""
    return {"status": "ok"}


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """存活探针：只要进程还能响应就算通过。"""
    return {"status": "alive"}


async def _check_db() -> dict:
    """主库试跑 ``SELECT 1``：带回测时延；失败摘错误摘要，不上抛。"""
    try:
        started = time.perf_counter()
        async with db.get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    except Exception as exc:  # noqa: BLE001 - 探针只记录状态，异常不外抛
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"[:200]}


def _check_queue() -> dict:
    """RAG 队列 Worker 实况：一个都不活（没启动或全挂了）才算失败。"""
    expected = max(1, get_settings().rag_queue_workers)
    alive = rag_queue.get_queue().workers_alive
    if alive == 0:
        return {"status": "error", "detail": f"无存活 Worker（期望 {expected}）"}
    status = "ok" if alive >= expected else "degraded"
    return {"status": status, "alive": alive, "expected": expected}


@router.get("/health/ready")
async def readiness():
    """就绪探针：真查依赖（DB + RAG 队列），任一不可用返 503。

    成功体走统一信封；503 时用独立 JSON（非 2xx 本就不包信封），
    body 里保留各分项状态，方便人一眼看出坏在哪项。
    """
    checks = {"db": await _check_db(), "rag_queue": _check_queue()}
    healthy = all(c["status"] in ("ok", "degraded") for c in checks.values())
    body = {"status": "ready" if healthy else "unready", "checks": checks}
    if healthy:
        return body
    return JSONResponse(status_code=503, content=body)
