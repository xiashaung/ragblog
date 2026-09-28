"""安全响应头中间件：给所有 HTTP 响应补上浏览器侧的基础防线。

为什么放在最外层注册：这些头对**一切**响应都该生效——包括 401/429 错误体、
/metrics、SPA 兜底页，越靠外越不会被内层谁"忘了发"钻空子。

补的四类头（都是行业默认值，不是可调项，故不进 config）：

- ``X-Content-Type-Options: nosniff``：禁掉浏览器 MIME 猜测，防把上传的
  文本文件当脚本执行的经典套路。
- ``X-Frame-Options: SAMEORIGIN``：管理控制台与博客都只许同源 iframe，
  挡住"套壳钓鱼"（把 /admin 嵌进假站骗点击）。
- ``Referrer-Policy: strict-origin-when-cross-origin``：跳外链只带 origin，
  不把带查询参数的完整 URL（可能含 token）送进别人站的 Referer。
- ``Permissions-Policy``：显式关掉相机/麦克风/地理位置，页面用不到就不给。

HSTS 只在确认是 HTTPS 直连/反代透传 ``x-forwarded-proto: https`` 时才发；
纯 http 的本地环境发了反而埋坑（浏览器强制跳 https 打不开）。
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_DEFAULT_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"SAMEORIGIN"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
)

_HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")


def _is_https(scope: Scope) -> bool:
    """判定"这是 HTTPS"：直连 scheme 或反代透传的 x-forwarded-proto。"""
    if scope.get("scheme") == "https":
        return True
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-proto":
            return value.split(b",")[0].strip().lower() == b"https"
    return False


class SecurityHeadersMiddleware:
    """纯 ASGI：在 ``http.response.start`` 上补缺省安全头（已有值不覆盖）。"""

    def __init__(self, app: ASGIApp) -> None:
        """存下中间件链的下家。"""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """非 http（websockets/lifespan）原样放行。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        extras = (_HSTS,) if _is_https(scope) else ()

        async def send_wrapper(message: Message) -> None:
            """response.start 时按「缺才补」注入头部。"""
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for pair in _DEFAULT_HEADERS + extras:
                    if pair[0] not in present:
                        headers.append(pair)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)
