"""Application entry point.

Run locally with:
    uvicorn app.main:app --reload

Static frontend: build ``web/dist`` (``npm --prefix web run build``) and
it is automatically served at ``/`` (single-process production deploy).
In dev use the Vite dev server on :5173 (proxies /api to :8000).
"""

import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import db
from .config import get_settings
from .responses import (
    _http_exception_handler,
    _validation_exception_handler,
    _value_error_handler,
)
from .routers import admin, ai, analytics, datasources, health, items, upload, users
from .schemas.envelope import ApiResponse
from .services import rag_queue


class _EnvelopeMiddleware(BaseHTTPMiddleware):
    """Guarantee every successful 2xx response is a uniform envelope.

    Wraps the response body: for ``2xx`` JSON responses it re-serialises
    the payload as ``{"code": 0, "data": <payload>, "message": "success"}``;
    a payload already shaped like an envelope is left untouched; non-JSON
    bodies (streaming chat, etc.) pass through unchanged. Error bodies are
    built by the exception handlers in ``app.responses`` and therefore
    bypass this middleware.

    Kept dependency-free (plain ASGI) so it never breaks streaming routes
    that already return chunked bodies.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_code = 200
        headers: list[tuple[bytes, bytes]] = []
        sent_body = b""

        async def send_wrapped(message: dict) -> None:
            nonlocal status_code, headers, sent_body

            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                sent_body = sent_body + (message.get("body", b"") or b"")

        # Invoke the downstream app, redirecting its `send` so we can
        # capture the (single-chunk) body. We replay our own `start`,
        # then the (envelope-wrapped) body.
        await self.app(scope, receive, send_wrapped)

        raw = sent_body
        if 200 <= status_code < 300 and sent_body:
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = None
            # Only wrap actual JSON objects/arrays in the envelope.
            # Non-JSON bodies (HTML from the SPA fallback, plain text,
            # already-enveloped strings, etc.) must pass through untouched —
            # otherwise the SPA's index.html would be turned into
            # {"code":0,"data":"<!doctype html ...>"}.
            if isinstance(parsed, dict):
                if "code" in parsed and "message" in parsed:
                    pass  # already an envelope — leave untouched
                else:
                    parsed = ApiResponse.ok(parsed)
                raw = json.dumps(
                    parsed.model_dump(), ensure_ascii=False
                ).encode("utf-8")

        # Drop stale length/encoding so clients trust the new body.
        kept = [
            (n, v) for n, v in headers if n.lower() not in (b"content-length",)
        ]

        await send({"type": "http.response.start", "status": status_code, "headers": kept})
        await send(
            {"type": "http.response.body", "body": raw, "more_body": False}
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: init DB engine + create tables (dev) / run alembic (prod).
    db.init_db()
    await db.init_models()
    # 启动 RAG 任务队列
    await rag_queue.start_queue()
    yield
    # Shutdown: dispose the engine + stop RAG queue
    await rag_queue.stop_queue()
    await db.dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description="A standard FastAPI project",
        debug=settings.debug,
        lifespan=lifespan,
    )
    application.add_exception_handler(HTTPException, _http_exception_handler)
    application.add_exception_handler(ValueError, _value_error_handler)
    application.add_exception_handler(
        RequestValidationError, _validation_exception_handler
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Wrap every successful 2xx **object** body in the uniform
    # {code, data, message} envelope. JSON arrays and primitives are left as
    # they are (so ``GET /admin/roles`` etc. return a bare list); non-JSON
    # bodies (SPA HTML, streaming chat) pass through untouched. Error bodies
    # are already built by the exception handlers in app.responses, so this
    # only touches 2xx responses.
    application.add_middleware(_EnvelopeMiddleware)

    application.include_router(health.router, prefix="/api/v1", tags=["health"])
    application.include_router(users.router, prefix="/api/v1/users", tags=["users"])
    application.include_router(items.router, prefix="/api/v1/items", tags=["items"])
    application.include_router(ai.router, prefix="/api/v1/ai", tags=["ai"])
    application.include_router(
        analytics.router, prefix="/api/v1/analytics", tags=["analytics"]
    )
    application.include_router(admin.router, tags=["admin"])
    application.include_router(datasources.router)
    application.include_router(upload.router, prefix="/api/v1", tags=["upload"])

    # --- Uploaded media (images) served straight from disk ---
    # 上传的图片落盘到 ``settings.media_root``，这里挂为静态目录，浏览器可直接
    # 通过 ``/media/uploads/xxx.png`` 查看，无需把 base64 存进数据库。
    # 先建目录再挂载（StaticFiles 对不存在目录会报错），并放在 SPA 兵底路由之前。
    media_root = settings.media_root
    media_root.mkdir(parents=True, exist_ok=True)
    (media_root / "uploads").mkdir(parents=True, exist_ok=True)
    application.mount(
        settings.media_url,
        StaticFiles(directory=str(media_root)),
        name="media",
    )

    # --- Static frontend (web/dist) ---
    # The dist directory is optional: in dev, use the Vite dev server
    # (port 5173) which proxies /api to this backend. When present, the
    # FastAPI app serves the built SPA at / for single-process deploys.
    dist = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "dist"
    )
    if os.path.isdir(dist):
        # /assets/* -> web/dist/assets/*
        application.mount(
            "/assets",
            StaticFiles(directory=os.path.join(dist, "assets")),
            name="assets",
        )

        # SPA fallback: any non-/api path serves index.html so that
        # client-side routes (if any) work after a refresh.

        # Root path must be matched explicitly: FastAPI's {full_path:path}
        # wildcard binds segments only, so "/" would otherwise fall through
        # to some GET route and return a JSON envelope instead of the SPA.
        @application.get("/", include_in_schema=False)
        async def root_index():
            return FileResponse(os.path.join(dist, "index.html"))

        @application.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str):
            backend_prefixes = ("api/", "admin/", "health")
            if full_path.startswith(backend_prefixes):
                raise HTTPException(status_code=404)
            candidate = os.path.join(dist, full_path)
            if full_path and os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(os.path.join(dist, "index.html"))

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", reload=True, host="0.0.0.0", port=8011)
