"""Uniform API response envelope for every route.

All endpoints — across every router — return the same three-field
structure so front-end code can treat every response uniformly:

    success -> { "code": 0,  "data": { ... }, "message": "success" }
    failure -> { "code": 401, "data": null,  "message": "<中文文案>" }

* ``code`` is ``0`` on success, otherwise the HTTP status code (401,
  404, 409, 422, ...). It is intentionally *decoupled* from the actual
  HTTP status code so business logic can branch on ``code === 0`` in
  JavaScript and on ``code == 0`` in Python, independent of transport.
* ``data`` carries the payload on success and is ``null`` on failure.
* ``message`` is a short, human-readable string; by default "success".

``FastAPI`` is configured with ``response_model_cls=ApiResponse`` so a
plain ``return some_object`` from any route is automatically wrapped —
routers never build the envelope by hand.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """Standard envelope wrapping every route payload."""

    code: int = 0
    data: T | None = None
    message: str = "success"

    @staticmethod
    def ok(data: T = None, message: str = "success") -> ApiResponse[T]:
        """Build a success envelope (``code == 0``)."""
        return ApiResponse(code=0, data=data, message=message)

    @staticmethod
    def error(code: int, message: str) -> ApiResponse[None]:
        """Build a failure envelope; ``data`` defaults to ``None``."""
        return ApiResponse(code=code, data=None, message=message)


__all__ = ["ApiResponse"]
