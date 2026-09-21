"""把各类异常转成统一的 ``ApiResponse`` 错体（与成功体同一形状）。

由 :func:`app.main.create_app` 注册，三个处理器盖住路由可能抛出的全部错：

* ``HTTPException`` —— 状态码与 ``detail`` 直接透到信封里；
* ``ValueError`` —— 业务层校验失败（如「没有有效问答对」），一律 400；
* ``RequestValidationError`` —— pydantic 参数校验失败，返 422。

关键约定：**只改响应体、不改 HTTP 状态码**，传输层行为保持原样（鉴权、
缓存、重试等语义不变），而体内的 ``code`` 与 HTTP 状态码一致、不拿它当业务码用。
前端在 axios 拦截器里把 ``data`` 读空、取 ``message`` 弹窗（见 web-vben/src/api/request.ts）。
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .schemas.envelope import ApiResponse

# 常见状态码 → 中文兜底文案，只用于路由没给 detail 的情形；
# 需要说清业务原因的路由自己 raise HTTPException 带中文 message，不要改这张表。
_STATUS_MESSAGES: dict[int, str] = {
    400: "请求参数有误",
    401: "登录失效，请重新登录",
    403: "拒绝访问",
    404: "资源不存在",
    409: "状态冲突",
    422: "参数校验失败",
    429: "请求过于频繁，请稍后再试",
    500: "服务器内部错误",
}


def _message_for_status(status_code: int) -> str:
    """状态码 → 中文兜底文案（路由未给 detail 时用）。"""
    return _STATUS_MESSAGES.get(status_code, f"请求失败（状态码 {status_code}）")


async def _http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    """路由里主动 raise 的业务错：状态码不变，``detail`` 当 message 透出。"""
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.error(exc.status_code, str(exc.detail)).model_dump(),
    )


async def _value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
    """服务层抛 ``ValueError(中文原因)`` 的统一出口，全部当 400。

    录入类接口（文件/文本/问答对/URL）的校验失败都走这里，所以不必在每个
    router 里手写一遍 HTTPException。
    """
    message = str(exc).strip() or _STATUS_MESSAGES[400]
    return JSONResponse(
        status_code=400,
        content=ApiResponse.error(400, message).model_dump(),
    )


async def _validation_exception_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体/参数不符合 pydantic 模型（包括 provider 名字拼错）→ 422。

    刻意不逐个列出字段错误：前端只要知道「参数不对」就能提示，详细定位靠后端日志。
    """
    return JSONResponse(
        status_code=422,
        content=ApiResponse.error(422, _STATUS_MESSAGES[422]).model_dump(),
    )


__all__ = [
    "_http_exception_handler",
    "_value_error_handler",
    "_validation_exception_handler",
    "_message_for_status",
]
