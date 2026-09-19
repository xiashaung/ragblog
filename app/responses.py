"""Exception handlers that convert errors into the shared ``ApiResponse``.

Registered on the app via :func:`create_app`. Two handlers cover every
error a route can raise:

* ``HTTPException`` — map the status code to a fixed Chinese message.
* ``ValueError`` — a domain-level validation failure (e.g. "no such
  admin"), surfaced as ``code == 400`` with a friendly message.

Both return :class:`~app.schemas.envelope.ApiResponse.error(...)` so the
client sees a uniform ``{code, data:null, message}`` shape regardless of
which router raised the error. The actual HTTP status code is *not*
changed — only the body — keeping transport behaviour intact while the
``code`` inside the body stays stable and decoupled.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .schemas.envelope import ApiResponse

# Fixed status-code -> Chinese message. Extensions go here only; routes
# that need domain-specific detail raise HTTPException with a custom
# message instead of relying on the map.
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
    return _STATUS_MESSAGES.get(status_code, f"请求失败（状态码 {status_code}）")


async def _http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.error(exc.status_code, str(exc.detail)).model_dump(),
    )


async def _value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
    message = str(exc).strip() or _STATUS_MESSAGES[400]
    return JSONResponse(
        status_code=400,
        content=ApiResponse.error(400, message).model_dump(),
    )


async def _validation_exception_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
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
