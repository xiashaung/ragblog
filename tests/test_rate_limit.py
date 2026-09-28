"""限流测试：LoginLimiter（登录/引导）+ SlidingWindowLimiter（公开博客 AI 成本闸）。

登录侧验「尝试窗 + 失败锁定」语义与 login/onboard 入口的 429；博客 AI 侧验通用
滑窗的计数 / 隔离，以及 /blog/* 端点越界即 429（带 Retry-After）、开关关掉后放行。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import rate_limit

# 与 config 默认值同名的一把假 settings：单测里不依赖真实配置
_NS = dict(
    login_window_seconds=60,
    login_max_attempts=3,
    login_lock_threshold=2,
    login_lockout_seconds=900,
)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch):
    """可控时钟：拨表测试窗口滑动，不用真等 60 秒。"""
    state = {"t": 1000.0}
    monkeypatch.setattr(rate_limit.LoginLimiter, "_now", staticmethod(lambda: state["t"]))
    return state


def _settings() -> SimpleNamespace:
    return SimpleNamespace(**_NS)


def test_attempt_window_blocks_and_slides(clock, monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = rate_limit.LoginLimiter()
    s = _settings()
    for _ in range(3):
        assert limiter.retry_after("k", settings=s) == 0
        limiter.record_attempt("k")
    wait = limiter.retry_after("k", settings=s)
    assert wait > 0  # 窗口打满
    clock["t"] += 61  # 滑出窗口
    assert limiter.retry_after("k", settings=s) == 0


def test_failure_lockout_survives_attempt_window(clock) -> None:
    limiter = rate_limit.LoginLimiter()
    s = _settings()
    limiter.record_failure("k")
    limiter.record_failure("k")
    clock["t"] += 61  # 尝试窗滑出了，锁定窗（900s）还没
    assert limiter.retry_after("k", settings=s) > 0
    clock["t"] += 900
    assert limiter.retry_after("k", settings=s) == 0


def test_success_clears_failures_only(clock) -> None:
    limiter = rate_limit.LoginLimiter()
    s = _settings()
    limiter.record_attempt("k")
    limiter.record_failure("k")
    limiter.record_success("k")
    assert limiter._failures.get("k") is None  # 失败清零
    assert len(limiter._attempts["k"]) == 1  # 尝试窗保留：成功不该成为绕过窗口的洞
    assert limiter.retry_after("k", settings=s) == 0  # 1 次尝试未达窗上限


async def test_login_locks_out_after_repeated_failures(client) -> None:
    """连错 5 次（config 默认阈值）后，第 6 次直接 429，不再碰口令校验。"""
    for _ in range(5):
        res = await client.post(
            "/api/v1/admin/login", json={"username": "ghost", "password": "wrong-pass"}
        )
        assert res.status_code == 401
    res = await client.post(
        "/api/v1/admin/login", json={"username": "ghost", "password": "wrong-pass"}
    )
    assert res.status_code == 429
    assert "Retry-After" in res.headers


async def test_login_limit_is_per_username(client) -> None:
    """锁定按「IP+用户名」计：换用户名不受另一个账号锁定的影响。"""
    for _ in range(5):
        await client.post(
            "/api/v1/admin/login", json={"username": "victim", "password": "wrong"}
        )
    res = await client.post(
        "/api/v1/admin/login", json={"username": "someone-else", "password": "wrong"}
    )
    assert res.status_code == 401  # 不是 429


async def test_onboard_key_guessing_gets_locked(client) -> None:
    """引导密钥猜测同样吃失败锁定（5 次 403 后第 6 次 429）。"""
    for _ in range(5):
        res = await client.post(
            "/api/v1/admin/onboard",
            json={"username": "hacker", "password": "whatever-123"},
            headers={"X-API-Key": "guess"},
        )
        assert res.status_code == 403
    res = await client.post(
        "/api/v1/admin/onboard",
        json={"username": "hacker", "password": "whatever-123"},
        headers={"X-API-Key": "guess"},
    )
    assert res.status_code == 429


# ---------------- 通用滑窗：SlidingWindowLimiter 单元 ----------------


def test_sliding_window_blocks_over_limit() -> None:
    lim = rate_limit.SlidingWindowLimiter()
    for _ in range(3):
        ok, _wait = lim.allow("k", window_seconds=60, max_requests=3)
        assert ok
    ok, wait = lim.allow("k", window_seconds=60, max_requests=3)
    assert not ok
    assert wait > 0


def test_sliding_window_isolates_keys_and_reset_clears() -> None:
    lim = rate_limit.SlidingWindowLimiter()
    lim.allow("a", window_seconds=60, max_requests=1)
    # 另一个 key 不受影响
    ok, _ = lim.allow("b", window_seconds=60, max_requests=1)
    assert ok
    # a 已满
    ok, _ = lim.allow("a", window_seconds=60, max_requests=1)
    assert not ok
    # 清零后 a 重得机会
    lim.reset()
    ok, _ = lim.allow("a", window_seconds=60, max_requests=1)
    assert ok


# ---------------- 公开博客 AI 端点：端到端 ----------------


def _tiny_limit(monkeypatch: pytest.MonkeyPatch, *, enabled=True, max_requests=2) -> None:
    """把 blog 路由读到的 settings 换成小阈值假对象（其余逻辑不变）。"""
    from app.routers import blog as blog_module

    fake = SimpleNamespace(
        blog_ai_rate_limit_enabled=enabled,
        blog_ai_window_seconds=60,
        blog_ai_max_requests=max_requests,
    )
    monkeypatch.setattr(blog_module, "get_settings", lambda: fake)


async def test_blog_answer_rate_limited(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _tiny_limit(monkeypatch, max_requests=2)
    for _ in range(2):
        r = await client.post("/api/v1/blog/answer", json={"query": "x"})
        assert r.status_code == 200, r.text
    r = await client.post("/api/v1/blog/answer", json={"query": "x"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


async def test_limit_shared_across_ai_endpoints(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """三个端点共用同一个 IP 窗口：search 打满后 answer 也进不了。"""
    _tiny_limit(monkeypatch, max_requests=2)
    assert (await client.post("/api/v1/blog/search", json={"query": "x"})).status_code == 200
    assert (await client.post("/api/v1/blog/answer", json={"query": "x"})).status_code == 200
    r = await client.post("/api/v1/blog/search", json={"query": "x"})
    assert r.status_code == 429


async def test_limit_disabled_passes_through(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _tiny_limit(monkeypatch, enabled=False, max_requests=1)
    for _ in range(5):
        r = await client.post("/api/v1/blog/answer", json={"query": "x"})
        assert r.status_code == 200, r.text
