"""/metrics 端点与计数器的测试：HTTP 计数、文本格式、令牌门。"""

from app.config import Settings
from app.routers import metrics as metrics_router


async def test_metrics_exposes_http_request_counts(client) -> None:
    """先打一发 /health，再抓 /metrics：应能看到对应路由模板的计数行。"""
    assert (await client.get("/api/v1/health")).status_code == 200
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # 响应是纯文本，没被 {code,data,message} 信封污染
    assert "# TYPE http_requests_total counter" in body
    # route 标签取自 APIRoute 自己的 path（不含 include_router 的前缀），
    # 参数化路由会归一成模板，不会把基数打爆
    assert 'http_requests_total{method="GET",route="/health",status="200"}' in body
    assert "# TYPE http_request_duration_seconds histogram" in body
    assert 'http_request_duration_seconds_bucket{le="+Inf"}' in body
    assert "process_uptime_seconds" in body


async def test_metrics_requires_token_when_configured(client, monkeypatch) -> None:
    """配了令牌后三种走法：不带 401、带头对、Bearer 对。"""

    monkeypatch.setattr(
        metrics_router,
        "get_settings",
        lambda: Settings(_env_file=None, metrics_token="sekret"),
    )
    resp = await client.get("/metrics")
    assert resp.status_code == 401
    resp = await client.get("/metrics", headers={"X-Metrics-Token": "sekret"})
    assert resp.status_code == 200
    resp = await client.get("/metrics", headers={"Authorization": "Bearer sekret"})
    assert resp.status_code == 200
    resp = await client.get("/metrics", headers={"X-Metrics-Token": "wrong"})
    assert resp.status_code == 401
