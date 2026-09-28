"""集中日志配置 + 请求 ID：全进程一套格式，日志能按请求串起来。

两件事：

1. :func:`setup_logging`：``create_app`` 里调用一次，把 app 自己的、uvicorn 的
   （error/access）日志统一到一个控制台格式下。``disable_existing_loggers``
   关掉，不动第三方库已建好的 logger；uvicorn 启动时也会配一遍自己的日志，
   但本函数在 ``app.main`` 导入时才跑（更晚），以其为准覆盖成这里的格式。
2. :class:`RequestIdMiddleware`：纯 ASGI 中间件，为每个请求生成（或沿用客户端
   传来的）``X-Request-ID``，写进 contextvar——格式化器从那里取值，同一请求
   内的所有日志行都带上同一个 rid，响应头也回给客户端一份便于排查。
   contextvar 天然按任务隔离，并发请求互不串号；后台队列等无请求上下文处
   取到的是缺省值 ``-``。
"""

from __future__ import annotations

import logging
import logging.config
import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# 当前请求的 ID；非请求上下文（启动、后台任务）里为空串，格式化时显示成 ``-``
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] [rid:%(request_id)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _RequestIdFormatter(logging.Formatter):
    """在格式化前把 contextvar 里的请求 ID 塞进 record。

    做成 Formatter 而不是 Filter：少一个 dictConfig 里的注册环节，且凡是
    走这套格式的行保证有 ``request_id`` 字段，不会因漏挂 filter 而 KeyError。
    """

    def format(self, record: logging.LogRecord) -> str:
        record.request_id = request_id_var.get() or "-"
        return super().format(record)


def setup_logging(level: str = "INFO") -> None:
    """统一配置根日志与 uvicorn 的三个 logger（幂等，可在 create_app 里放心调）。

    app.* 的业务日志走 root handler；uvicorn 系列原本各挂各的 handler，这里
    一并接管成同一格式（access 日志保留，排查线上问题全靠它）。
    """
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "app": {
                    "()": _RequestIdFormatter,
                    "fmt": _LOG_FORMAT,
                    "datefmt": _DATE_FORMAT,
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "app",
                    "stream": "ext://sys.stderr",
                },
            },
            "root": {"level": level, "handlers": ["console"]},
            "loggers": {
                # propagate 到 root 即可，不给它们单独挂 handler（会打两遍）
                "uvicorn": {"level": level, "propagate": True, "handlers": []},
                "uvicorn.error": {"level": level, "propagate": True, "handlers": []},
                "uvicorn.access": {"level": level, "propagate": True, "handlers": []},
            },
        }
    )


class RequestIdMiddleware:
    """为每个 http 请求划定请求 ID：进 contextvar、出响应头 ``X-Request-ID``。

    客户端（网关/前端）带了就沿用——整条链路共用一个 ID 才有排查价值；
    没带则服务端造一个。只认长度合理的头值，过长的当成攻击噪声丢弃重造。
    """

    MAX_INBOUND_LEN = 128

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        inbound = Headers(scope=scope).get("x-request-id") or ""
        request_id = (
            inbound
            if 0 < len(inbound) <= self.MAX_INBOUND_LEN
            else uuid.uuid4().hex
        )
        token = request_id_var.set(request_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (b"x-request-id", request_id.encode()),
                    ],
                }
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            request_id_var.reset(token)


__all__ = [
    "RequestIdMiddleware",
    "request_id_var",
    "setup_logging",
]
