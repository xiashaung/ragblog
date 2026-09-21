"""健康检查路由：给探针 / 负载均衡用，由 ``create_app`` 挂在 ``/api/v1`` 下。

三个接口目前都只返固定状态，**没有**真的去探数据库或下游：要拿它们做
可靠的存活/就绪判别时，得自己把依赖探测加到对应的函数里。
"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """通用健康检查：能响应就返 ok。"""
    return {"status": "ok"}


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """存活探针：只要进程还能响应就算通过。"""
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness() -> dict[str, str]:
    """就绪探针：流量可以切过来（现在与存活同义，未检依赖）。"""
    return {"status": "ready"}
