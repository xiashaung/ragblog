"""博客路由：后台管理（``/admin/blogs``）与前台公开（``/api/v1/blog``）两套。

拆法是一个文件两个 ``APIRouter``，各自带完整 prefix、由 ``main`` 无 prefix 注册
（与 admin / datasources 同一约定）。鉴权口径：

- 管理侧全部走 RBAC：看列表要 ``blog:read``，改动内容（含发布 / 下线）要
  ``blog:manage``（写隐含读，见 ``app/rbac.py``）；
- 公开侧无任何鉴权，但只出已发布文章；AI 问答 / 对话是匿名接口，
  会话不持久化（前端自带历史），长度上限见 ``schemas.blog``。

发布动作会真调嵌入模型重建该文章的索引（与知识库同一条链路），失败时
服务层抛 ``ValueError`` 由全局处理器转 400，中文原因原样透出。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .. import deps, services
from ..db import get_db
from ..rbac import Permission
from ..schemas.blog import (
    BlogAnswerResponse,
    BlogCategoryCreate,
    BlogCategoryItem,
    BlogCategoryListResponse,
    BlogCategoryUpdate,
    BlogChatChunk,
    BlogChatRequest,
    BlogPostCreate,
    BlogPostDetail,
    BlogPostItem,
    BlogPostListResponse,
    BlogPostUpdate,
    BlogPublicCategoryItem,
    BlogPublicCategoryListResponse,
    BlogPublicDetail,
    BlogPublicListResponse,
    BlogSearchRequest,
    BlogSearchResponse,
    BlogTagItem,
    BlogTagListResponse,
)
from ..services.llm import get_llm
from ..services.rbac_service import Principal

admin_router = APIRouter(prefix="/admin/blogs", tags=["blog-admin"])
public_router = APIRouter(prefix="/api/v1/blog", tags=["blog"])

logger = logging.getLogger(__name__)


# ---------------- 后台管理 ----------------


@admin_router.get("", response_model=BlogPostListResponse)
async def list_posts(
    keyword: str | None = Query(default=None, max_length=200),
    post_status: str | None = Query(default=None, alias="status", pattern="^(draft|published)$"),
    tag: str | None = Query(default=None, max_length=30),
    category_id: str | None = Query(default=None, max_length=64, description="按分类 ID 过滤"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.BLOG_READ)),
) -> dict:
    """文章分页列表（含草稿）：关键字匹配标题与摘要，标签/分类做过滤。"""
    return await services.blog.list_posts(
        session,
        keyword=keyword,
        status=post_status,
        tag=tag,
        category_id=category_id,
        offset=offset,
        limit=limit,
    )


@admin_router.post("", response_model=BlogPostItem, status_code=status.HTTP_201_CREATED)
async def create_post(
    req: BlogPostCreate,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.BLOG_MANAGE)),
) -> dict:
    """新建文章（先落草稿；发布是显式动作，见 publish）。"""
    return await services.blog.create_post(session, req)


# 分类路由要注在 /{post_id} 之前：GET /admin/blogs/categories 会被
# 「/admin/blogs/{post_id}」的形吃掉，FastAPI 按注册顺序匹配。
@admin_router.get("/categories", response_model=BlogCategoryListResponse)
async def list_categories(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.BLOG_READ)),
) -> BlogCategoryListResponse:
    """分类列表（含全部文章数的占用统计）：按 sort_order 排序。"""
    items = await services.blog.list_categories(session)
    return BlogCategoryListResponse(items=items)


@admin_router.post(
    "/categories", response_model=BlogCategoryItem, status_code=status.HTTP_201_CREATED
)
async def create_category(
    req: BlogCategoryCreate,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.BLOG_MANAGE)),
) -> BlogCategoryItem:
    """新建分类；重名 / slug 冲突时服务层抛 ValueError 转 400。"""
    return await services.blog.create_category(session, req)


@admin_router.patch("/categories/{category_id}", response_model=BlogCategoryItem)
async def update_category(
    category_id: str,
    req: BlogCategoryUpdate,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.BLOG_MANAGE)),
) -> BlogCategoryItem:
    """改分类名称 / slug / 排序（文章归属不受影响）。"""
    data = await services.blog.update_category(session, category_id, req)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分类不存在")
    return data


@admin_router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: str,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.BLOG_MANAGE)),
) -> None:
    """删分类；下面还有文章时拒绝（400，需先重新归类）。"""
    if not await services.blog.delete_category(session, category_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分类不存在")


@admin_router.get("/{post_id}", response_model=BlogPostDetail)
async def get_post(
    post_id: str,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.BLOG_READ)),
) -> dict:
    """单篇详情（含正文 Markdown）。"""
    data = await services.blog.get_post(session, post_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文章不存在")
    return data


@admin_router.patch("/{post_id}", response_model=BlogPostItem)
async def update_post(
    post_id: str,
    req: BlogPostUpdate,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.BLOG_MANAGE)),
) -> dict:
    """局部更新；已发布文章改了标题 / 正文会自动重同步索引。"""
    data = await services.blog.update_post(session, post_id, req)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文章不存在")
    return data


@admin_router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post(
    post_id: str,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.BLOG_MANAGE)),
) -> None:
    """删文章，连带清掉它同步出的知识文档。"""
    if not await services.blog.delete_post(session, post_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文章不存在")


@admin_router.post("/{post_id}/publish", response_model=BlogPostItem)
async def publish_post(
    post_id: str,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.BLOG_MANAGE)),
) -> dict:
    """发布：正文同步进知识库（要调嵌入模型），前台可见、可被 AI 检索。"""
    data = await services.blog.publish_post(session, post_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文章不存在")
    return data


@admin_router.post("/{post_id}/unpublish", response_model=BlogPostItem)
async def unpublish_post(
    post_id: str,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.BLOG_MANAGE)),
) -> dict:
    """下线：从知识库摘掉索引，回到草稿状态。"""
    data = await services.blog.unpublish_post(session, post_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文章不存在")
    return data


# ---------------- 前台公开 ----------------


@public_router.get("/posts", response_model=BlogPublicListResponse)
async def public_posts(
    tag: str | None = Query(default=None, max_length=30),
    category: str | None = Query(default=None, max_length=80, description="按分类 slug 过滤"),
    featured: bool = Query(default=False, description="只看置顶"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1, le=50),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """首页 / 标签页 / 分类页列表：只有已发布文章，按发布时间倒序。"""
    return await services.blog.list_published(
        session,
        tag=tag,
        category=category,
        featured=featured or None,
        offset=offset,
        limit=limit,
    )


@public_router.get("/categories", response_model=BlogPublicCategoryListResponse)
async def public_categories(
    session: AsyncSession = Depends(get_db),
) -> BlogPublicCategoryListResponse:
    """分类导航：只出有已发布文章的分类（空分类不进菜单）。"""
    items = await services.blog.list_categories(session, published_only=True)
    return BlogPublicCategoryListResponse(
        items=[
            BlogPublicCategoryItem(name=c.name, slug=c.slug, post_count=c.post_count)
            for c in items
            if c.post_count > 0
        ]
    )


@public_router.get("/tags", response_model=BlogTagListResponse)
async def public_tags(
    session: AsyncSession = Depends(get_db),
) -> BlogTagListResponse:
    """标签云：已发布文章的标签聚合（按使用次数倒序）。"""
    items: list[BlogTagItem] = await services.blog.tag_counts(session)
    return BlogTagListResponse(items=items)


@public_router.get("/posts/{slug}", response_model=BlogPublicDetail)
async def public_post_detail(
    slug: str,
    session: AsyncSession = Depends(get_db),
) -> dict:
    """文章页：按 slug 取正文，成功访问计一次阅读数。"""
    data = await services.blog.get_published_by_slug(session, slug)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文章不存在或未发布")
    return data


@public_router.post("/search", response_model=BlogSearchResponse)
async def blog_search(
    req: BlogSearchRequest,
    session: AsyncSession = Depends(get_db),
) -> BlogSearchResponse:
    """AI 语义搜索：混合检索限定在已发布文章的索引里，命中片段带高亮摘要。"""
    return await services.blog.search_posts(session, req)


@public_router.post("/answer", response_model=BlogAnswerResponse)
async def blog_answer(
    req: BlogSearchRequest,
    session: AsyncSession = Depends(get_db),
) -> BlogAnswerResponse:
    """一次性 AI 问答：检索 + 组织回答 + 引用（无命中不调模型直接说明）。"""
    return await services.blog.answer_question(
        session, BlogSearchRequest(query=req.query, top_k=req.top_k)
    )


@public_router.post("/chat")
async def blog_chat(
    req: BlogChatRequest,
    session: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """AI 对话搜索：多轮流式（NDJSON），第一行带本轮检索到的引用文章。

    检索在返回流之前做完（生成器活不过请求级 DB session 的活不能留给流里），
    流中报错按既有惯例包成最后一行 chunk 透出。
    """
    citations, messages = await services.blog.prepare_chat(session, req)
    backend = get_llm(None, feature="rag")

    async def gen():
        """逐行边生边发：第一行先给引用，随后原样转发模型的每个增量。"""
        first = BlogChatChunk(citations=citations)
        yield json.dumps(first.model_dump(), ensure_ascii=False) + "\n"
        try:
            async for chunk in backend.chat_stream(messages):
                line = BlogChatChunk(**chunk.model_dump())
                yield json.dumps(line.model_dump(), ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001 - 流已开始，只能把错误包成末行 chunk
            logger.exception("博客对话流式中断")
            tail = BlogChatChunk(done=True, error=f"生成中断：{exc}")
            yield json.dumps(tail.model_dump(), ensure_ascii=False) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no"},
    )
