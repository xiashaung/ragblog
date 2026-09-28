"""进程内登录限流：滑动窗口计数 + 连续失败锁定。

为什么需要：``POST /api/v1/admin/login`` 原本可以无限次尝试，argon2 只是拖慢
不是防线。这里在路由层加两道轻量的闸：

1. **尝试窗口**：同一「IP + 用户名」在 ``login_window_seconds`` 内最多
   ``login_max_attempts`` 次登录尝试（无论成败），超限 429。
2. **失败锁定**：``login_lockout_seconds`` 内累计 ``login_lock_threshold`` 次
   口令错误，该组合直接锁定到窗口滑出为止；成功登录即清零。

诚实的边界（都是有意取舍，不是遗漏）：

- 状态在**进程内存**里：多 Worker 部署时每个进程各记各的，实际允许的重试
  次数约等于「配置值 × 进程数」。要全局精确得上 Redis，对这个量级的管理
  后台不值得。
- 客户端 IP 优先取 ``X-Forwarded-For`` 第一段（生产走 nginx 反代，否则所有
  请求都是 127.0.0.1，全世界共享同一个窗口会互相锁死）。XFF 可被伪造——但
  伪造者只会把限流打在自己头上，对限流这个用途无害。
"""

from __future__ import annotations

import time
from collections import deque

from fastapi import HTTPException, Request, status

# 内存防护：恶意用海量不同 key 灌记录时，超上限就先清扫过期项
_MAX_KEYS = 4096


class LoginLimiter:
    """按 key 记「尝试时间戳」与「失败时间戳」两条滑窗。"""

    def __init__(self) -> None:
        """两张 deque 表：key → 秒级时间戳队列。"""
        self._attempts: dict[str, deque[float]] = {}
        self._failures: dict[str, deque[float]] = {}

    def reset(self) -> None:
        """清空全部计数（测试之间互不串染的逃生门）。"""
        self._attempts.clear()
        self._failures.clear()

    @staticmethod
    def _now() -> float:
        """单调时钟：改系统时间不能用来提前解锁。"""
        return time.monotonic()

    def _prune(self, q: deque[float], window: float, now: float) -> None:
        """丢窗旧记录：deque 按时间入队，从左边弹就够了。"""
        cutoff = now - window
        while q and q[0] < cutoff:
            q.popleft()

    def _sweep_if_bloated(self) -> None:
        """key 数超限时的粗清：直接全丢。误伤表现为个别 IP 重得一次机会。"""
        if len(self._attempts) > _MAX_KEYS or len(self._failures) > _MAX_KEYS:
            self.reset()

    def retry_after(self, key: str, *, settings) -> float:
        """现在发起会被拦的话，还要等多久（秒）；不被拦返回 0。

        两道闸取更严的一道：窗口尝试数打满 → 等最早那次滑出；失败锁定 →
        等最早那次失败滑出锁定窗。
        """
        now = self._now()
        wait = 0.0
        q = self._attempts.get(key)
        if q is not None:
            self._prune(q, settings.login_window_seconds, now)
            if len(q) >= settings.login_max_attempts:
                wait = max(wait, q[0] + settings.login_window_seconds - now)
        f = self._failures.get(key)
        if f is not None:
            self._prune(f, settings.login_lockout_seconds, now)
            if len(f) >= settings.login_lock_threshold:
                wait = max(wait, f[0] + settings.login_lockout_seconds - now)
        return wait

    def record_attempt(self, key: str) -> None:
        """登记一次登录尝试（成败都算），用于尝试窗口。"""
        self._sweep_if_bloated()
        now = self._now()
        q = self._attempts.setdefault(key, deque())
        self._prune(q, 3600.0, now)  # 上限远大于窗口，防 deque 无界增长
        q.append(now)

    def record_failure(self, key: str) -> None:
        """登记一次口令错误，累计到阈值即锁定该 key。"""
        self._sweep_if_bloated()
        now = self._now()
        q = self._failures.setdefault(key, deque())
        self._prune(q, 3600.0, now)
        q.append(now)

    def record_success(self, key: str) -> None:
        """登录成功清零失败计数；尝试窗口保留（否则窗口闸可被绕穿）。"""
        self._failures.pop(key, None)


# 模块级单例：与 app/metrics.py 同思路，限流状态本来就该全局共享
login_limiter = LoginLimiter()


class SlidingWindowLimiter:
    """通用「每 key 每窗最多 N 次」滑窗计数——给昂贵端点当成本预算闸。

    与 :class:`LoginLimiter` 分开：那个绑死了「尝试窗 + 失败锁定」的登录语义，
    这里只做纯计数，谁调都一样的第一性用途。状态同样只在**进程内存**里，
    多 Worker 部署各记各的，实际阈值约等于「配置值 ÷ 进程数」（护栏够用即可，
    要全局精确得上 Redis，对这个量级不值得）。
    """

    def __init__(self) -> None:
        """key → 放行时间戳队列（只记被放行的，拦下的不计入，免得持续轰把封锁无限延长）。"""
        self._hits: dict[str, deque[float]] = {}

    def reset(self) -> None:
        """清空全部计数（测试之间互不串染的逃生门）。"""
        self._hits.clear()

    @staticmethod
    def _now() -> float:
        """单调时钟：改系统时间不能用来提前解锁。"""
        return time.monotonic()

    def _prune(self, q: deque[float], window: float, now: float) -> None:
        """丢窗旧记录：deque 按时间入队，从左边弹就够了。"""
        cutoff = now - window
        while q and q[0] < cutoff:
            q.popleft()

    def _sweep_if_bloated(self) -> None:
        """key 数超限时的粗清：直接全丢。误伤表现为个别来源重得一次机会。"""
        if len(self._hits) > _MAX_KEYS:
            self.reset()

    def allow(self, key: str, *, window_seconds: int, max_requests: int) -> tuple[bool, float]:
        """打一次并判定是否放行（检查与记录原子完成，无 TOCTOU）。

        返回 ``(放行?, 需等待秒数)``：窗口内未满则记一次并放行；已满则不记录、
        返回距最早那次放行滑出窗口还要等的秒数（向上取整交由调用方）。
        """
        now = self._now()
        self._sweep_if_bloated()
        q = self._hits.setdefault(key, deque())
        self._prune(q, window_seconds, now)
        if len(q) >= max_requests:
            return False, max(0.0, q[0] + window_seconds - now)
        q.append(now)
        return True, 0.0


# 公开博客 AI 端点（/blog/search|answer|chat）的成本闸：按访客 IP 计数
blog_ai_limiter = SlidingWindowLimiter()

# 控制台昂贵 AI 端点（/ai/chat、/ai/knowledge/answer、/analytics/query）的成本闸：
# 按登录账号 admin_id 计数，三个端点共用这一个窗口。
admin_ai_limiter = SlidingWindowLimiter()


def enforce_sliding_limit(
    limiter: SlidingWindowLimiter,
    key: str,
    *,
    enabled: bool,
    window_seconds: int,
    max_requests: int,
    message: str,
) -> None:
    """通用滑窗闸门：放行则静默返回，越界则抛 429 + ``Retry-After``。

    各路由读自己的 ``get_settings()`` 把阈值传进来（保持 monkeypatch 目标在路由
    模块上，单测可换小阈值假对象），本函数不碰配置、只做「一次计数 + 一次拦截」。
    开关关掉（``enabled=False``）时直接放行，供紧急解限流用。
    """
    if not enabled:
        return
    allowed, retry = limiter.allow(
        key, window_seconds=window_seconds, max_requests=max_requests
    )
    if not allowed:
        seconds = int(retry) + 1
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"{message}，请 {seconds} 秒后再试",
            headers={"Retry-After": str(seconds)},
        )


def client_key(request: Request, username: str = "") -> str:
    """拼限流 key：``IP|小写用户名``。onboard 等无用户名入口只用 IP 段。"""
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else ""
    if not ip:
        ip = request.client.host if request.client else "unknown"
    return f"{ip}|{username.strip().lower()}"
