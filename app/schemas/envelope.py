"""所有接口统一的响应信封。

不管哪个路由，体里都是同一套三字段，前端可以一律对待：

    成功 -> { "code": 0,   "data": { ... }, "message": "success" }
    失败 -> { "code": 401, "data": null,   "message": "<中文文案>" }

* ``code``：成功时为 ``0``，否则就是那个 HTTP 状态码（401 / 404 / 409 / 422 …）。
  它**只用于展示与判错**，不改传输层状态码（两者始终一致）。
* ``data``：成功时装业务数据，失败时为 ``null``。
* ``message``：给人看的一句话，默认 ``success``。

路由从不手工拼信封：成功体由 ``app.main._EnvelopeMiddleware`` 在 ASGI 层包上
（只包 2xx 的 JSON 对象），失败体由 ``app.responses`` 的异常处理器拼好。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """每个路由响应外头套的那一层。"""

    code: int = 0
    data: T | None = None
    message: str = "success"

    @staticmethod
    def ok(data: T = None, message: str = "success") -> ApiResponse[T]:
        """拼成功信封（``code == 0``）。"""
        return ApiResponse(code=0, data=data, message=message)

    @staticmethod
    def error(code: int, message: str) -> ApiResponse[None]:
        """拼失败信封；``data`` 恒为 ``None``。"""
        return ApiResponse(code=code, data=None, message=message)


__all__ = ["ApiResponse"]
