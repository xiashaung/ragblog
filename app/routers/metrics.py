"""/metrics 端点：把进程内指标按 Prometheus 文本格式暴露出去。

鉴权口径：配了 ``APP_METRICS_TOKEN`` 就必须带对（``Authorization: Bearer``
或 ``X-Metrics-Token`` 二选一），没配则直接放行——抓取器通常部署在内网，
但公网暴露时这把锁能挡住路由清单与流量规模的泄露。

响应是 ``text/plain``，信封中间件只包 ``application/json``，所以这里
天然不会被套进 ``{code,data,message}``，标准抓取器拿到就是合法 exposition。
"""

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .. import metrics
from ..config import get_settings

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_class=PlainTextResponse)
async def expose_metrics(request: Request) -> PlainTextResponse:
    """输出全量指标快照（一次抓取 = 一次进程内状态的瞬时照）。"""
    token = get_settings().metrics_token
    if token:
        provided = request.headers.get("x-metrics-token") or request.headers.get(
            "authorization", ""
        ).removeprefix("Bearer ").strip()
        # 定长比较：令牌的等值判断不用短路比较，避免计时侧信道
        if not secrets.compare_digest(provided, token):
            raise HTTPException(status_code=401, detail="metrics 令牌无效")
    return PlainTextResponse(metrics.render())
