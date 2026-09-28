"""登录限流测试：LoginLimiter 单元行为 + login/onboard 入口的 429。"""

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
