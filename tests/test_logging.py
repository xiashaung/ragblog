"""集中日志与请求 ID：中间件透传/生成 X-Request-ID，格式化器带出 rid。"""

import logging

from app.logging_setup import _RequestIdFormatter, request_id_var


async def test_request_id_passthrough(client) -> None:
    """客户端带了 X-Request-ID 就沿用：全链路共用一个 ID 才有排查价值。"""
    res = await client.get("/api/v1/health", headers={"X-Request-ID": "trace-abc-123"})
    assert res.headers["x-request-id"] == "trace-abc-123"


async def test_request_id_generated_per_request(client) -> None:
    """没带则每个请求各造一个，且响应头回给客户端。"""
    first = await client.get("/api/v1/health")
    second = await client.get("/api/v1/health")
    rid = first.headers.get("x-request-id")
    assert rid and len(rid) == 32
    assert second.headers.get("x-request-id") != rid


async def test_request_id_rejects_overlong_header(client) -> None:
    """过长的入站头值当噪声丢弃重造，不给日志与下游留注入面。"""
    res = await client.get("/api/v1/health", headers={"X-Request-ID": "x" * 300})
    assert res.headers["x-request-id"] != "x" * 300
    assert len(res.headers["x-request-id"]) == 32


def test_formatter_picks_request_id_from_contextvar() -> None:
    """请求上下文内打的日志行带 rid；上下文外显示 ``-``。"""
    fmt = _RequestIdFormatter("%(message)s|%(request_id)s")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    token = request_id_var.set("rid-42")
    try:
        assert fmt.format(record) == "hello|rid-42"
    finally:
        request_id_var.reset(token)
    assert fmt.format(record) == "hello|-"
