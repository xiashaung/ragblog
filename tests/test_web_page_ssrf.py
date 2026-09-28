"""SSRF 防护测试：web_page 的统一闸门（DNS 解析 + 逐跳重定向复检）。

不碰真实网络：域名解析 monkeypatch 掉 web_page._resolve，HTTP 请求
monkeypatch 掉 httpx.AsyncClient，重点验证「内网请求根本不会被发出去」。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.services import web_page

# ---------------- ip_denied：地址段判定 ----------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "127.9.9.9",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "169.254.169.254",  # 云元数据服务
        "0.0.0.0",
        "::1",
        "::ffff:127.0.0.1",  # IPv4 映射的 IPv6
        "fc00::1",
        "224.0.0.1",
        "240.0.0.1",
    ],
)
def test_ip_denied_blocks_internal_ranges(ip: str) -> None:
    assert web_page.ip_denied(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:2800:220:1::", "example.com"])
def test_ip_denied_allows_public(ip: str) -> None:
    # 非 IP 字符串（是域名）也返回 False，由上层决定是否再去解析
    assert not web_page.ip_denied(ip)


# ---------------- assert_safe_target：入口闸门 ----------------


async def test_safe_target_rejects_ip_literals() -> None:
    for url in (
        "http://127.0.0.1:8011/api/v1/health",
        "http://10.1.2.3/internal",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/x",
    ):
        with pytest.raises(ValueError):
            await web_page.assert_safe_target(url)


async def test_safe_target_rejects_internal_hostnames() -> None:
    for url in (
        "http://localhost:8011/admin",
        "http://myhost.internal/x",
        "http://anything.local/x",
    ):
        with pytest.raises(ValueError):
            await web_page.assert_safe_target(url)


async def test_safe_target_rejects_domain_pointing_at_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """核心新增防线：公网域名的 A 记录指向 127.0.0.1 也要拦。"""

    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    monkeypatch.setattr(web_page, "_resolve", fake_resolve)
    with pytest.raises(ValueError, match="解析到内网"):
        await web_page.assert_safe_target("https://evil.example.com/x")


async def test_safe_target_allows_public_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(web_page, "_resolve", fake_resolve)
    target = await web_page.assert_safe_target("https://example.com/a")
    assert target == "https://example.com/a"


async def test_safe_target_allow_private_opt_in_skips_checks() -> None:
    # 显式放开（内网部署调试）时连回环也放行，但 scheme 白名单仍然生效
    target = await web_page.assert_safe_target("http://127.0.0.1:8011", allow_private=True)
    assert target.startswith("http://127.0.0.1")
    with pytest.raises(ValueError):
        await web_page.assert_safe_target("file:///etc/passwd", allow_private=True)


# ---------------- fetch_page：逐跳重定向复检 ----------------


class _FakeClient:
    """假 httpx.AsyncClient：按脚本应答，并记录所有真正发起过的 URL。"""

    visits: list[str] = []
    script: dict[str, tuple[int, dict[str, str]]] = {}

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def get(self, url: str) -> httpx.Response:
        type(self).visits.append(str(url))
        status, headers = self.script.get(str(url), (404, {}))
        request = httpx.Request("GET", url)
        body = b"<html><body>" + b"x" * 40 + b"</body></html>"
        return httpx.Response(status, headers=headers, content=body, request=request)


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    _FakeClient.visits = []
    _FakeClient.script = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    async def public_resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(web_page, "_resolve", public_resolve)
    return _FakeClient


async def test_fetch_page_refuses_redirect_to_internal(fake_http: type[_FakeClient]) -> None:
    """公网站点 302 拐向内网：内网请求必须根本不被发出（不是「跟完再作废」）。"""
    fake_http.script = {
        "https://example.com/start": (302, {"location": "http://127.0.0.1:8011/admin"}),
    }
    with pytest.raises(ValueError, match="内网"):
        await web_page.fetch_page("https://example.com/start")
    assert fake_http.visits == ["https://example.com/start"]  # 只访问过起点


async def test_fetch_page_refuses_internal_dns_rebinding(
    fake_http: type[_FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """重定向落点是域名、但解析到内网：同样要在发起请求前拦下。"""
    fake_http.script = {
        "https://example.com/start": (302, {"location": "http://rebind.example/steal"}),
    }
    real_resolve = web_page._resolve

    async def resolve_by_host(host: str, port: int) -> list[str]:
        return ["169.254.169.254"] if host == "rebind.example" else await real_resolve(host, port)

    monkeypatch.setattr(web_page, "_resolve", resolve_by_host)
    with pytest.raises(ValueError, match="解析到内网"):
        await web_page.fetch_page("https://example.com/start")
    assert fake_http.visits == ["https://example.com/start"]


async def test_fetch_page_follows_clean_redirects(fake_http: type[_FakeClient]) -> None:
    fake_http.script = {
        "https://example.com/start": (302, {"location": "https://cdn.example/doc"}),
        "https://cdn.example/doc": (200, {"content-type": "text/html"}),
    }
    page = await web_page.fetch_page("https://example.com/start")
    assert page.final_url == "https://cdn.example/doc"
    assert page.status == 200
    assert fake_http.visits == ["https://example.com/start", "https://cdn.example/doc"]


async def test_fetch_page_stops_redirect_loop(fake_http: type[_FakeClient]) -> None:
    fake_http.script = {
        "https://example.com/a": (302, {"location": "https://example.com/b"}),
        "https://example.com/b": (302, {"location": "https://example.com/a"}),
    }
    with pytest.raises(ValueError, match="重定向"):
        await web_page.fetch_page("https://example.com/a")
