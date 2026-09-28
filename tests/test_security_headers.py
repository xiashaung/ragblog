"""安全响应头测试：一切响应（含错误体与 /metrics）都要带上基础防线头。"""

from __future__ import annotations

_BASE_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "SAMEORIGIN",
    "referrer-policy": "strict-origin-when-cross-origin",
}


async def test_success_carries_security_headers(client) -> None:
    res = await client.get("/api/v1/health")
    assert res.status_code == 200
    for name, value in _BASE_HEADERS.items():
        assert res.headers.get(name) == value
    assert "permissions-policy" in res.headers
    # 本地 http 环境不发 HSTS：发了会逼浏览器强制跳 https，纯 http 部署直接打不开
    assert "strict-transport-security" not in res.headers


async def test_error_responses_carry_headers_too(client) -> None:
    """401 也不能漏——最外层注册的意义就在这。"""
    res = await client.get("/api/v1/admin/me")
    assert res.status_code == 401
    for name, value in _BASE_HEADERS.items():
        assert res.headers.get(name) == value


async def test_metrics_endpoint_carries_headers_too(client) -> None:
    res = await client.get("/metrics")
    assert res.status_code == 200
    assert res.headers.get("x-content-type-options") == "nosniff"


async def test_hsts_when_forwarded_proto_https(client) -> None:
    res = await client.get("/api/v1/health", headers={"x-forwarded-proto": "https"})
    assert res.status_code == 200
    assert "max-age=31536000" in res.headers.get("strict-transport-security", "")
