"""进程内指标收集 + Prometheus 文本暴露：不引第三方库，够用就好。

设计取舍：

- 指标量很小，直接用几个模块级对象（Counter/Gauge/Histogram）记账， asyncio
  单线程事件循环内自增不需要锁；进程重启即清零（不做持久化聚合，真要历史
  曲线交给抓取侧的 Prometheus/VictoriaMetrics 存）。
- HTTP 请求按「路由模板」打标签而不是原始路径——否则 ``/knowledge/{id}``
  这种路径会把标签基数打爆，抓取端聚合也失去意义。
- 暴露格式按 Prometheus exposition 规范手写 ``# HELP / # TYPE / 样本行``，
  任何标准抓取器都能直接吃。

调用点：``main.create_app`` 挂 :class:`MetricsMiddleware`；LLM 重试/降级
（``services/llm.py``）与 RAG 任务终态（``services/rag_queue.py``）在各自
记日志的地方顺手 ``inc()`` 一下。
"""

from __future__ import annotations

import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# 进程启动时刻（uptime 指标用）
_STARTED_AT = time.time()


class Counter:
    """带标签的单调计数器。标签值元组 -> 累计值；只在事件循环内自增，无锁。"""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, labels: tuple[str, ...] = (), amount: float = 1.0) -> None:
        self._values[labels] = self._values.get(labels, 0.0) + amount

    def value(self, labels: tuple[str, ...] = ()) -> float:
        """当前累计值（测试与自检用；未记过账返回 0）。"""
        return self._values.get(labels, 0.0)

    def render(self) -> list[str]:
        lines = [
            f"# HELP {self.name} {self.help}",
            f"# TYPE {self.name} counter",
        ]
        for labels, value in self._values.items():
            lines.append(f"{self.name}{_labels_str(self.label_names, labels)} {_num(value)}")
        return lines


class Gauge:
    """可增可减的瞬时值（如在途请求数、进程存活）。"""

    def __init__(self, name: str, help_text: str, value: float = 0.0) -> None:
        self.name = name
        self.help = help_text
        self.value = value

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        self.value -= amount

    def render(self) -> list[str]:
        return [
            f"# HELP {self.name} {self.help}",
            f"# TYPE {self.name} gauge",
            f"{self.name} {_num(self.value)}",
        ]


class Histogram:
    """固定桶的耗时分布（无标签；按请求维度统计进程整体就够）。

    桶边界覆盖 50ms~10s：交互接口看前几档，LLM/RAG 这类慢调用看后几档。
    """

    BUCKETS: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help = help_text
        self.count = 0
        self.total = 0.0
        self._bucket_counts = [0] * len(self.BUCKETS)

    def observe(self, seconds: float) -> None:
        self.count += 1
        self.total += seconds
        for i, upper in enumerate(self.BUCKETS):
            if seconds <= upper:
                self._bucket_counts[i] += 1

    def render(self) -> list[str]:
        lines = [
            f"# HELP {self.name} {self.help}",
            f"# TYPE {self.name} histogram",
        ]
        # Prometheus 约定：le 桶是累计计数（小于等于该边界）
        cumulative = 0
        for upper, delta in zip(self.BUCKETS, self._bucket_counts, strict=True):
            cumulative += delta
            lines.append(
                f'{self.name}_bucket{{le="{_num(upper)}"}} {cumulative}'
            )
        lines.append(f'{self.name}_bucket{{le="+Inf"}} {self.count}')
        lines.append(f"{self.name}_sum {_num(self.total)}")
        lines.append(f"{self.name}_count {self.count}")
        return lines


def _labels_str(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not names:
        return ""
    inner = ",".join(f'{n}="{v}"' for n, v in zip(names, values, strict=True))
    return "{" + inner + "}"


def _num(value: float) -> str:
    """样本值格式化：整数不带小数点，浮点保留有效位。"""
    if value == int(value):
        return str(int(value))
    return f"{value:.6g}"


# --- 全进程指标实例（要加新指标就在这里登记）---

http_requests_total = Counter(
    "http_requests_total",
    "HTTP 请求总数（按方法/路由模板/状态码）",
    ("method", "route", "status"),
)
http_in_progress = Gauge("http_requests_in_progress", "当前正在处理的 HTTP 请求数")
http_request_duration = Histogram(
    "http_request_duration_seconds", "HTTP 请求处理耗时分布（秒）"
)
llm_retries_total = Counter("llm_retries_total", "LLM 调用因瞬时故障发生的重试次数")
llm_fallback_total = Counter(
    "llm_fallback_total", "非流式对话从主 provider 降级到备胎的次数"
)
rag_tasks_finished_total = Counter(
    "rag_tasks_finished_total", "RAG 后台任务完成数（按终态）", ("status",)
)

_ALL: list[Any] = [
    http_requests_total,
    http_in_progress,
    http_request_duration,
    llm_retries_total,
    llm_fallback_total,
    rag_tasks_finished_total,
]


def render() -> str:
    """输出全部指标的 Prometheus 文本格式（末尾换行是格式要求）。"""
    lines: list[str] = [
        "# HELP process_uptime_seconds 进程启动至今的秒数",
        "# TYPE process_uptime_seconds gauge",
        f"process_uptime_seconds {_num(time.time() - _STARTED_AT)}",
    ]
    for metric in _ALL:
        lines.extend(metric.render())
    return "\n".join(lines) + "\n"


class MetricsMiddleware:
    """纯 ASGI 计时/计数中间件。

    状态码从 response.start 消息里取；路由模板在请求处理完后由 Router
    回填进同一个 scope dict，所以在 finally 里拿得到。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = "0"

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = str(message["status"])
            await send(message)

        http_in_progress.inc()
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - started
            http_in_progress.dec()
            # Starlette 路由命中的 endpoint 会把 route 写回同一个 scope dict；
            # 没命中（404）时取不到，用 unmatched 兜住基数。
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            http_requests_total.inc((scope.get("method", "?"), template, status))
            http_request_duration.observe(duration)


__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "MetricsMiddleware",
    "http_in_progress",
    "http_request_duration",
    "http_requests_total",
    "llm_fallback_total",
    "llm_retries_total",
    "rag_tasks_finished_total",
    "render",
]
