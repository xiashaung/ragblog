"""应用入口：装配 FastAPI 实例、注册路由与中间件、挂前端静态产物。

本地跑法：
    uvicorn app.main:app --reload

启动与一次请求依次经过的环节（也是本文件从上到下的阅读顺序）：
    路由 lifespan（建表 + 加载模型配置 + 起 RAG 队列）
    → CORSMiddleware（来路过滤）
    → _EnvelopeMiddleware（把 2xx 响应包成 {code,data,message}）
    → 各 router（app/routers/*，依赖注入见 app/deps.py）
    → 异常处理器（app/responses.py，错误体也同一格式）

前端：两个独立 Vite 项目，构建后由本进程一并托管，单进程即可部署：
    - 博客前台 ``web-blog/dist`` 挂根路径 /（入口即首页，见 web-blog vite base=/）
    - 管理控制台 ``web/dist`` 挂 /admin（vite base=/admin/，管理 API 在 /api/v1/admin）
开发期各自用 Vite dev server（:5174 / :5173，把 /api 代理到后端）。
"""

import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import db, services
from .config import get_settings
from .logging_setup import RequestIdMiddleware, setup_logging
from .metrics import MetricsMiddleware
from .responses import (
    _http_exception_handler,
    _validation_exception_handler,
    _value_error_handler,
)
from .routers import (
    admin,
    ai,
    analytics,
    blog,
    datasources,
    health,
    items,
    system_config,
    upload,
    users,
)
from .routers import (
    metrics as metrics_router,
)
from .schemas.envelope import ApiResponse
from .security_headers import SecurityHeadersMiddleware
from .services import rag_queue

logger = logging.getLogger(__name__)


class _EnvelopeMiddleware(BaseHTTPMiddleware):
    """把所有成功的 2xx JSON **对象**响应统一包成 ``{code, data, message}`` 信封。

    看到 ``http.response.start`` 时按 content-type 判定这条响应怎么走：
    - ``application/json`` 的 2xx 才攒齐 body 重组信封（这类本就是单次 JSONResponse，
      不存在流式）；已是信封形状的不动，裸数组/标量原样过（否则前端会多套一层
      data 嵌套，``providers.filter(...)`` 这类写法全坏）；
    - 其余（流式对话的 ``application/x-ndjson``、SPA 的 HTML、文件、非 2xx）**逐块
      原样透传**，一块都不攒。

    为什么必须分流：若对流式响应也「攒齐再发」，就把流式退化成「等全部生成完再
    一次性返回」——这正是「换了云端模型后前端仍要等整个 chat 回答、看不到逐字输出」
    的根因（mock 瞬间返完所以此前没暴露）。错误响应体由 app/responses.py 的异常处理
    器自己拼好，本中间件不改动它（非 2xx 直接透传）。

    形式上继承 ``BaseHTTPMiddleware`` 只为拿到 ``app`` 引用，真正的活全在重写的
    :meth:`__call__` 里：不走基类的 ``dispatch``，下家的 ``send`` 整个由本类接管。
    """

    def __init__(self, app: ASGIApp) -> None:
        """只存下中间件链的下家（``self.app``），本类自己包办 ``__call__``。"""
        super().__init__(app)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """非 http 事件（websockets 等）直接放行；http 请求按响应类型分流。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        wrap = False  # 这条响应是否要攒 body 包信封（仅 application/json 的 2xx 候选）
        status_code = 200
        headers: list[tuple[bytes, bytes]] = []
        sent_body = b""

        async def send_wrapped(message: dict) -> None:
            """替下游的 ``send``：按 content-type 决定「攒齐包信封」还是「逐块透传」。"""
            nonlocal wrap, status_code, headers, sent_body

            if message["type"] == "http.response.start":
                ctype = b""
                for name, value in message.get("headers", []):
                    if name.lower() == b"content-type":
                        ctype = value.lower()
                        break
                status_code = message["status"]
                headers = list(message.get("headers", []))
                wrap = 200 <= status_code < 300 and b"application/json" in ctype
                if not wrap:
                    # 流式/文件/HTML/错误响应：start 原样转发，后续 body 逐块透传
                    await send(message)
                # wrap 时先扣住 start，等 body 收齐、重算 content-length 后再发
                return

            if not wrap:
                await send(message)  # 透传后续 body/trailers，一块都不攒
                return

            if message["type"] == "http.response.body":
                sent_body = sent_body + (message.get("body", b"") or b"")
                if message.get("more_body", False):
                    return  # 还没收完（单次 JSONResponse 不会进这里）
            else:
                return  # http.response.trailers 之类：包信封后不保留

            raw = sent_body
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = None
            # 只包真正的 JSON 对象；裸数组与标量原样过。
            if isinstance(parsed, dict):
                if "code" in parsed and "message" in parsed:
                    pass  # 已是信封——不动
                else:
                    parsed = ApiResponse.ok(parsed)
                raw = json.dumps(
                    parsed.model_dump(), ensure_ascii=False
                ).encode("utf-8")

            # 丢掉旧的 content-length，否则客户端会拿它去校验新响应体。
            kept = [
                (n, v) for n, v in headers if n.lower() not in (b"content-length",)
            ]
            await send({"type": "http.response.start", "status": status_code, "headers": kept})
            await send(
                {"type": "http.response.body", "body": raw, "more_body": False}
            )

        await self.app(scope, receive, send_wrapped)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """进程生命周期：启动时建表并预热配置，退出时停队列并释放连接池。"""
    # 启动：建数据库引擎 + 建表（开发环境；生产跑 alembic upgrade head）
    db.init_db()
    await db.init_models()
    # 把「模型接入 / 系统配置」里的 provider 选择读进内存：聊天、嵌入、精排
    # 实际用哪个端点与模型名以数据库为准，.env 只在没配置时兜底，
    # 所以后台改完不需要重启。
    async with db.get_session_factory()() as session:
        await services.system_config.SystemConfig.init_defaults(session)
        await services.provider_store.refresh(session)
    # 启动 RAG 任务队列
    await rag_queue.start_queue()
    yield
    # 关闭：先停后台任务，再释放连接池
    await rag_queue.stop_queue()
    await db.dispose_engine()


def create_app() -> FastAPI:
    """装配应用：异常处理器 → 中间件 → 路由 → 静态资源，顺带 SPA 兜底。

    路由前缀约定：所有后端 API 都在 ``/api/v1`` 下（admin 体系是 /api/v1/admin）；
    ``/admin`` 路径留给管理控制台 SPA 页面，两者不能再互相串门。
    新加路由时别在 router 里重复写 prefix，否则会拼出双层路径。
    """
    settings = get_settings()
    # 日志先配好，后面启动环节的 warning（如 CORS 守卫）才能走统一格式
    setup_logging(settings.log_level)
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
    # CORS：``allow_credentials=True`` 与通配符 origin 是不能同时成立的组合——
    # 浏览器会直接拒收带 Access-Control-Allow-Credentials: * 的响应（开了也白开），
    # 而通配 + 凭据语义上又等于欢迎任意站带 cookie 来打。因此检测到通配时
    # 强制关掉凭据模式并留警告：要携 cookie，就把来源一个个列进 APP_CORS_ORIGINS。
    cors_origins = settings.cors_origins
    allow_credentials = True
    if "*" in cors_origins:
        allow_credentials = False
        logger.warning(
            "CORS 配置含通配符 origin，已强制关闭 allow_credentials；"
            "需要携带凭据的跨域请求请把具体来源写进 APP_CORS_ORIGINS"
        )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 把每个成功的 2xx **对象** 响应体统一包成 {code, data, message} 信封。
    # JSON 数组与标量保持原样（所以 ``GET /api/v1/admin/roles`` 等仍返裸列表），非 JSON 体
    # （SPA 的 HTML、流式对话）不动。错误响应体已由 app.responses 的异常处理器拼好，
    # 因此本中间件只碰 2xx。
    application.add_middleware(_EnvelopeMiddleware)
    # 请求 ID 中间件最后注册 = 最外层执行：不管响应最内层是谁发的，
    # X-Request-ID 头都能补上；contextvar 也从这里开始覆盖整个请求周期。
    application.add_middleware(RequestIdMiddleware)
    # 指标中间件再套一层（最外层）：耗时与状态码统计覆盖包括信封、
    # CORS 预检在内的完整 ASGI 路径。
    application.add_middleware(MetricsMiddleware)
    # 安全响应头最后注册 = 真正最外层：错误体、/metrics、SPA 兜底页
    # 也能带上 nosniff / X-Frame-Options 这些基础防线。
    application.add_middleware(SecurityHeadersMiddleware)

    application.include_router(health.router, prefix="/api/v1", tags=["health"])
    # /metrics 挂根路径（不在 /api/v1 下）：Prometheus 抓取配置惯例路径，
    # 也方便反代层单独给抓取器开白名单。
    application.include_router(metrics_router.router)
    application.include_router(ai.router, prefix="/api/v1/ai", tags=["ai"])
    application.include_router(
        analytics.router, prefix="/api/v1/analytics", tags=["analytics"]
    )
    # 管理 API 自带 /api/v1/admin 前缀（/admin 留给控制台 SPA）
    application.include_router(admin.router, tags=["admin"])
    # 博客：后台管理（/api/v1/admin/blogs）与前台公开 API（/api/v1/blog）
    application.include_router(blog.admin_router)
    application.include_router(blog.public_router)
    application.include_router(system_config.router, prefix="/api/v1")
    application.include_router(datasources.router)
    application.include_router(upload.router, prefix="/api/v1", tags=["upload"])

    # 示例 CRUD（users/items）：无鉴权的模板接口，只在 debug 下挂载，
    # 避免生产环境白暴露一张可增删改的演示表。
    if settings.debug:
        application.include_router(
            users.router, prefix="/api/v1/users", tags=["users"]
        )
        application.include_router(
            items.router, prefix="/api/v1/items", tags=["items"]
        )

    # --- 上传的图片（直接从磁盘对外提供）---
    # 上传的图片落盘到 ``settings.media_root``，这里挂为静态目录，浏览器可直接
    # 通过 ``/media/uploads/xxx.png`` 查看，无需把 base64 存进数据库。
    # 先建目录再挂载（StaticFiles 对不存在目录会报错），并放在 SPA 兜底路由之前。
    media_root = settings.media_root
    media_root.mkdir(parents=True, exist_ok=True)
    (media_root / "uploads").mkdir(parents=True, exist_ok=True)
    application.mount(
        settings.media_url,
        StaticFiles(directory=str(media_root)),
        name="media",
    )

    # --- 前端托管：博客前台挂 /，管理控制台挂 /admin ---
    # 两个 Vite 产物与 API 同进程托管。注册顺序即匹配顺序：API 路由与 /media
    # 已全部注册在上面，下面的 SPA 兜底是最后才轮到的兼容项。
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    blog_dist = os.path.join(root_dir, "web-blog", "dist")
    admin_dist = os.path.join(root_dir, "web", "dist")

    # 管理控制台（web-vben 产物，vite base=/admin/，资源在 /admin/assets 下）。
    # 页面路由（/admin/roles 等）与 API 不再冲突的前提：管理 API 全部收进了
    # /api/v1/admin（见 app/routers/admin.py），这里只接住页面与静态文件。
    if os.path.isdir(admin_dist):
        admin_assets = os.path.join(admin_dist, "assets")
        if os.path.isdir(admin_assets):
            application.mount(
                "/admin/assets",
                StaticFiles(directory=admin_assets),
                name="admin-assets",
            )

        @application.get("/admin", include_in_schema=False)
        @application.get("/admin/", include_in_schema=False)
        async def admin_index():
            """控制台入口：/admin 与 /admin/ 都回管理台 index.html。"""
            return FileResponse(os.path.join(admin_dist, "index.html"))

        @application.get("/admin/{full_path:path}", include_in_schema=False)
        async def admin_spa(full_path: str):
            """控制台 SPA 兜底：命中真实文件（favicon 等）直接给，否则回客户端路由。"""
            candidate = os.path.join(admin_dist, full_path)
            if full_path and os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(os.path.join(admin_dist, "index.html"))

    # 旧 /blog 链接重定向：博客入口已改挂根路径，历史分享链 302 到对应新地址。
    @application.get("/blog", include_in_schema=False)
    @application.get("/blog/", include_in_schema=False)
    async def blog_redirect_root():
        return RedirectResponse("/", status_code=302)

    @application.get("/blog/{full_path:path}", include_in_schema=False)
    async def blog_redirect(full_path: str):
        return RedirectResponse(f"/{full_path}", status_code=302)

    # 博客前台（web-blog 产物，vite base=/，资源在 /assets 下）。
    if os.path.isdir(blog_dist):
        blog_assets = os.path.join(blog_dist, "assets")
        if os.path.isdir(blog_assets):
            application.mount(
                "/assets",
                StaticFiles(directory=blog_assets),
                name="blog-assets",
            )

        @application.get("/", include_in_schema=False)
        async def blog_index():
            """站点根路径即博客首页；API 文档在 /docs（注册更早，不会被这里吃掉）。"""
            return FileResponse(os.path.join(blog_dist, "index.html"))

        @application.get("/{full_path:path}", include_in_schema=False)
        async def blog_spa(full_path: str):
            """博客 SPA 兜底：未命中的 api/health 路径返 404，其余回博客首页。"""
            if full_path.startswith(("api/", "health")):
                raise HTTPException(status_code=404)
            candidate = os.path.join(blog_dist, full_path)
            if full_path and os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(os.path.join(blog_dist, "index.html"))

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", reload=True, host="0.0.0.0", port=8011)
