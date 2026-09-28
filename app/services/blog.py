"""个人博客服务层：文章 CRUD、发布同步、公开查询与 AI 检索的三个入口。

设计核心是「发布 = 进知识库」：文章发布时把 Markdown 正文经
:func:`rag.upsert_document` 同步成一份知识文档（meta.kind == "blog"），
ID 记在 ``BlogPost.knowledge_id``；下线 / 删除连带删文档，重新编辑已发布
文章会自动再同步一次。检索不建第二套向量表，前台 AI 搜索通过
``rag.search(restrict_doc_ids=...)`` 把召回限定在已发布文章的文档集合里。

公开侧只有读接口：AI 问答 / 对话不落库（匿名会话由前端自己携带历史），
所以这里没有会话持久化函数。
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Sequence

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import media_gc
from ..models import BlogCategory, BlogPost
from ..schemas.ai import ChatMessage, KnowledgeSearchRequest
from ..schemas.blog import (
    BlogAnswerResponse,
    BlogCategoryCreate,
    BlogCategoryItem,
    BlogCategoryUpdate,
    BlogChatRequest,
    BlogCitation,
    BlogPostCreate,
    BlogPostUpdate,
    BlogSearchRequest,
    BlogSearchResponse,
    BlogTagItem,
)
from . import llm, rag
from .markdown_text import strip_markdown

STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"

#: 摘要自动摘取的长度（剥掉 Markdown 语法后的正文字符数）
SUMMARY_CHARS = 120
#: 检索分片 → 文章的去重放宽系数：先按 chunk 宽召回，再塌缩到文章裁 top_k
CHUNKS_PER_POST = 2
#: 回喂对话模型的历史消息条数上限（system 与本轮提问之外最多带这么多条）
CHAT_HISTORY_LIMIT = 10

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

BLOG_CHAT_SYSTEM_PROMPT = (
    "你是一个个人博客的检索问答助手。请基于下方提供的文章片段（Context）与对话历史作答，"
    "并遵守以下要求：\n"
    "1. 先整合片段内容，用自己的话组织成简洁清晰的回答，不要整段照抄原文；\n"
    "2. 引用了某个片段的结论时在句末标注来源编号 [n]（对应 Context 中的编号）；\n"
    "3. 只依据 Context 作答，Context 里没有的信息要明确说明「博客中未找到相关文章」，"
    "不要编造，也不要联网补充。"
)


# --- 内部小工具 ---


def _slugify(text: str) -> str:
    """标题 → URL 片段：只保留 ASCII 字母数字，其余压成单个连字符。

    纯中文标题会剥成空串，调用方负责兜底（见 :func:`_unique_slug`）。
    """
    return _SLUG_STRIP.sub("-", text.strip().lower()).strip("-")[:150]


async def _slug_taken(session: AsyncSession, slug: str, *, exclude_id: str | None = None) -> bool:
    stmt = select(BlogPost.id).where(BlogPost.slug == slug)
    if exclude_id:
        stmt = stmt.where(BlogPost.id != exclude_id)
    return (await session.scalar(stmt.limit(1))) is not None


async def _unique_slug(session: AsyncSession, wanted: str | None, title: str) -> str:
    """定稿 slug：显式给的 > 标题生成的 > 随机兜底；冲突时追加 -2/-3… 后缀。"""
    base = _slugify(wanted or "") or _slugify(title) or f"post-{uuid.uuid4().hex[:6]}"
    slug = base
    index = 1
    while await _slug_taken(session, slug):
        index += 1
        slug = f"{base}-{index}"
    return slug


def _auto_summary(content: str) -> str:
    """摘要留空时的兜底：剥掉 Markdown 语法、压成一行后截断。"""
    text = strip_markdown(content)
    return re.sub(r"\s+", " ", text).strip()[:SUMMARY_CHARS]


def _clean_row(row: BlogPost) -> dict:
    """ORM 行 → 响应字段（模型默认值可能为 None，出口统一补齐）。

    注意这里只出 ``category_id``；展示用的 ``category`` 摘要由各出口函数
    经 :func:`_with_category` 批量附上（避开逐行 N+1 查询）。
    """
    return {
        "id": row.id,
        "slug": row.slug,
        "title": row.title,
        "summary": row.summary or "",
        "cover_url": row.cover_url,
        "tags": [t for t in (row.tags or []) if isinstance(t, str)],
        "category_id": row.category_id,
        "status": row.status,
        "featured": bool(row.featured),
        "view_count": int(row.view_count or 0),
        "knowledge_id": row.knowledge_id,
        "published_at": row.published_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def _get_row(session: AsyncSession, post_id: str) -> BlogPost | None:
    return await session.get(BlogPost, post_id)


async def _remove_index(session: AsyncSession, row: BlogPost) -> None:
    """删掉文章同步出的知识文档并清引用（没同步过就是 no-op）。"""
    if row.knowledge_id:
        await rag.delete_knowledge(session, row.knowledge_id)
        row.knowledge_id = None


async def _sync_index(session: AsyncSession, row: BlogPost) -> int:
    """把文章当前正文重新同步进知识库，返回分片数。

    先删旧文档再 upsert：``upsert_document`` 的文档 ID 由标题派生，改了标题的
    话旧 ID 的索引会残留，必须先清掉。发布要求正文非空、标题在已发布文章里
    唯一（同标题会派生同一个文档 ID，互相覆盖索引）。
    """
    if not (row.content or "").strip():
        raise ValueError("正文为空，不能发布：请先补充文章内容")
    clash = await _get_by_title(session, row.title, exclude_id=row.id)
    if clash is not None and clash.status == STATUS_PUBLISHED:
        raise ValueError(f"已有已发布文章使用同一标题（{clash.slug}），请改标题后再发布")
    await _remove_index(session, row)
    result = await rag.upsert_document(
        session,
        title=row.title,
        content=row.content,
        metadata={"kind": "blog", "blog_id": row.id, "slug": row.slug},
        fmt="markdown",
    )
    row.knowledge_id = result.knowledge_id
    return result.chunks


async def _get_by_title(
    session: AsyncSession, title: str, *, exclude_id: str | None = None
) -> BlogPost | None:
    stmt = select(BlogPost).where(BlogPost.title == title)
    if exclude_id:
        stmt = stmt.where(BlogPost.id != exclude_id)
    return await session.scalar(stmt.limit(1))


# --- 文章出口的分类附加 ---


async def _categories_map(session: AsyncSession) -> dict[str, BlogCategory]:
    """全量分类按 ID 索引（分类表很小，一次拉取比逐行查省得多）。"""
    rows = (await session.scalars(select(BlogCategory))).all()
    return {r.id: r for r in rows}


def _attach_category(
    items: list[dict], cats: dict[str, BlogCategory], *, public: bool
) -> list[dict]:
    """给一批响应 dict 填 ``category`` 摘要；public=True 时顺手抹掉内部 ID。"""
    for d in items:
        cat = cats.get(d.get("category_id") or "")
        d["category"] = {"name": cat.name, "slug": cat.slug} if cat else None
        if public:
            d.pop("category_id", None)
    return items


async def _with_category(session: AsyncSession, data: dict, *, public: bool = False) -> dict:
    return _attach_category([data], await _categories_map(session), public=public)[0]


async def _with_category_list(
    session: AsyncSession, items: list[dict], *, public: bool = False
) -> list[dict]:
    return _attach_category(items, await _categories_map(session), public=public)


# --- 分类管理 ---


async def _get_category(session: AsyncSession, category_id: str) -> BlogCategory | None:
    return await session.get(BlogCategory, category_id)


async def _check_category_exists(session: AsyncSession, category_id: str) -> None:
    """文章挂分类前的存在性校验（无外键，这里把关；不存在转 400）。"""
    if await _get_category(session, category_id) is None:
        raise ValueError("分类不存在，请先创建或选择已有分类")


async def _category_name_taken(
    session: AsyncSession, name: str, *, exclude_id: str | None = None
) -> bool:
    stmt = select(BlogCategory.id).where(BlogCategory.name == name)
    if exclude_id:
        stmt = stmt.where(BlogCategory.id != exclude_id)
    return (await session.scalar(stmt.limit(1))) is not None


async def _category_slug_taken(
    session: AsyncSession, slug: str, *, exclude_id: str | None = None
) -> bool:
    stmt = select(BlogCategory.id).where(BlogCategory.slug == slug)
    if exclude_id:
        stmt = stmt.where(BlogCategory.id != exclude_id)
    return (await session.scalar(stmt.limit(1))) is not None


async def _unique_category_slug(
    session: AsyncSession, wanted: str | None, name: str, *, exclude_id: str | None = None
) -> str:
    """分类 slug 定稿：显式给的 > 名称生成的 > 随机兜底；冲突加 -2/-3… 后缀。"""
    base = (
        _slugify(wanted or "") or _slugify(name) or f"cat-{uuid.uuid4().hex[:6]}"
    )[:80]
    slug = base
    index = 1
    while await _category_slug_taken(session, slug, exclude_id=exclude_id):
        index += 1
        slug = f"{base}-{index}"[:80]
    return slug


def _category_item(row: BlogCategory, post_count: int) -> BlogCategoryItem:
    return BlogCategoryItem(
        id=row.id,
        name=row.name,
        slug=row.slug,
        sort_order=int(row.sort_order or 0),
        post_count=post_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def list_categories(
    session: AsyncSession, *, published_only: bool = False
) -> list[BlogCategoryItem]:
    """分类列表（后台全量 / 前台只数已发布）：按 sort_order、名称排序。"""
    rows = (
        await session.scalars(
            select(BlogCategory).order_by(BlogCategory.sort_order, BlogCategory.name)
        )
    ).all()
    count_stmt = (
        select(BlogPost.category_id, func.count())
        .where(BlogPost.category_id.is_not(None))
        .group_by(BlogPost.category_id)
    )
    if published_only:
        count_stmt = count_stmt.where(BlogPost.status == STATUS_PUBLISHED)
    counts = {cid: int(n) for cid, n in (await session.execute(count_stmt)).all() if cid}
    return [_category_item(r, counts.get(r.id, 0)) for r in rows]


async def create_category(
    session: AsyncSession, payload: BlogCategoryCreate
) -> BlogCategoryItem:
    """新建分类：名称唯一，slug 缺省时自动生成（中文名剥空则随机兜底）。"""
    name = payload.name.strip()[:50]
    if not name:
        raise ValueError("分类名称不能为空")
    if await _category_name_taken(session, name):
        raise ValueError(f"分类名称「{name}」已被占用")
    row = BlogCategory(
        name=name,
        slug=await _unique_category_slug(session, payload.slug, name),
        sort_order=payload.sort_order,
    )
    session.add(row)
    await session.flush()
    return _category_item(row, 0)


async def update_category(
    session: AsyncSession, category_id: str, payload: BlogCategoryUpdate
) -> BlogCategoryItem | None:
    """局部更新分类；不存在返 None（路由转 404），重名 / 撞 slug 抛 ValueError。"""
    row = await _get_category(session, category_id)
    if row is None:
        return None
    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields and (fields["name"] or "").strip():
        new_name = fields["name"].strip()[:50]
        if new_name != row.name and await _category_name_taken(
            session, new_name, exclude_id=row.id
        ):
            raise ValueError(f"分类名称「{new_name}」已被占用")
        row.name = new_name
    if "slug" in fields and fields["slug"]:
        wanted = _slugify(fields["slug"])[:80]
        if wanted and wanted != row.slug:
            if await _category_slug_taken(session, wanted, exclude_id=row.id):
                raise ValueError(f"slug「{wanted}」已被占用")
            row.slug = wanted
    if "sort_order" in fields and fields["sort_order"] is not None:
        row.sort_order = fields["sort_order"]
    row.updated_at = time.time()
    await session.flush()
    count = await session.scalar(
        select(func.count()).select_from(BlogPost).where(BlogPost.category_id == row.id)
    )
    return _category_item(row, int(count or 0))


async def delete_category(session: AsyncSession, category_id: str) -> bool:
    """删分类：有文章归属时拒绝（ValueError → 400），不静默改文章归属。"""
    row = await _get_category(session, category_id)
    if row is None:
        return False
    count = await session.scalar(
        select(func.count()).select_from(BlogPost).where(BlogPost.category_id == row.id)
    )
    if count:
        raise ValueError(f"分类「{row.name}」下还有 {int(count)} 篇文章，请先重新归类")
    await session.delete(row)
    await session.flush()
    return True


# --- 后台 CRUD ---


async def create_post(session: AsyncSession, payload: BlogPostCreate) -> dict:
    """新建文章（一律先落为草稿；发布是显式的 publish 动作）。"""
    if payload.category_id:
        await _check_category_exists(session, payload.category_id)
    row = BlogPost(
        slug=await _unique_slug(session, payload.slug, payload.title),
        title=payload.title.strip()[:200],
        summary=(payload.summary or "").strip() or _auto_summary(payload.content),
        content=payload.content,
        cover_url=payload.cover_url or None,
        tags=list(payload.tags),
        category_id=payload.category_id or None,
        featured=payload.featured,
        status=STATUS_DRAFT,
    )
    session.add(row)
    await session.flush()
    return await _with_category(session, _clean_row(row))


async def list_posts(
    session: AsyncSession,
    *,
    keyword: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    category_id: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> dict:
    """后台分页列表：标题/摘要模糊匹配，标签做包含匹配，分类按 ID 精确匹配。"""
    stmt = select(BlogPost)
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(BlogPost.title.ilike(like), BlogPost.summary.ilike(like))
        )
    if status:
        stmt = stmt.where(BlogPost.status == status)
    if category_id:
        stmt = stmt.where(BlogPost.category_id == category_id)
    if tag:
        # JSON 数组的包含查询跨方言写法不一，量级小，直接 Python 端过滤。
        rows = (
            await session.scalars(stmt.order_by(desc(BlogPost.updated_at)))
        ).all()
        rows = [r for r in rows if tag in (r.tags or [])]
        total = len(rows)
        page = rows[offset : offset + limit]
        items = await _with_category_list(session, [_clean_row(r) for r in page])
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": items,
        }

    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await session.execute(count_stmt)).scalar_one()
    rows = (
        await session.scalars(
            stmt.order_by(desc(BlogPost.updated_at)).offset(offset).limit(limit)
        )
    ).all()
    items = await _with_category_list(session, [_clean_row(r) for r in rows])
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": items,
    }


async def get_post(session: AsyncSession, post_id: str) -> dict | None:
    """后台详情（含正文）；不存在返 None 由路由转 404。"""
    row = await _get_row(session, post_id)
    if row is None:
        return None
    data = _clean_row(row)
    data["content"] = row.content or ""
    return await _with_category(session, data)


async def update_post(
    session: AsyncSession, post_id: str, payload: BlogPostUpdate
) -> dict | None:
    """局部更新文章。已发布文章改了标题 / 正文会同步重进索引（保持前台一致）。

    换封面 / 删正文图后旧文件按引用计数回收：新正文还引用的不会被误删，
    没有任何行再引用的才落刀（release 内部全库复查，不依赖这里的 diff）。
    """
    row = await _get_row(session, post_id)
    if row is None:
        return None
    old_media = [*media_gc.extract_markdown_images(row.content), *(row.cover_url or "",)]
    fields = payload.model_dump(exclude_unset=True)
    content_changed = False
    if "title" in fields and (fields["title"] or "").strip():
        new_title = fields["title"].strip()[:200]
        content_changed = content_changed or new_title != row.title
        row.title = new_title
    if "slug" in fields and fields["slug"]:
        wanted = _slugify(fields["slug"])
        if wanted and wanted != row.slug:
            if await _slug_taken(session, wanted, exclude_id=row.id):
                raise ValueError(f"slug「{wanted}」已被占用")
            row.slug = wanted
    if "summary" in fields:
        row.summary = (fields["summary"] or "").strip()
    if "content" in fields and fields["content"] is not None:
        content_changed = content_changed or fields["content"] != row.content
        row.content = fields["content"]
        if not row.summary:
            row.summary = _auto_summary(row.content)
    if "cover_url" in fields:
        row.cover_url = fields["cover_url"] or None
    if "tags" in fields and fields["tags"] is not None:
        row.tags = list(fields["tags"])
    if "category_id" in fields:
        # 没传 = 不改；显式 null / 空串 = 移出分类（exclude_unset 区分这两种）
        new_cat = fields["category_id"] or None
        if new_cat:
            await _check_category_exists(session, new_cat)
        row.category_id = new_cat
    if "featured" in fields and fields["featured"] is not None:
        row.featured = fields["featured"]
    row.updated_at = time.time()
    if row.status == STATUS_PUBLISHED and content_changed:
        await _sync_index(session, row)
    await session.flush()
    await media_gc.release_urls(session, old_media)
    return await _with_category(session, _clean_row(row))


async def delete_post(session: AsyncSession, post_id: str) -> bool:
    """删文章；连带删它同步出的知识文档，正文内嵌图与封面按引用计数回收。"""
    row = await _get_row(session, post_id)
    if row is None:
        return False
    old_media = [*media_gc.extract_markdown_images(row.content), *(row.cover_url or "",)]
    await _remove_index(session, row)
    await session.delete(row)
    await session.flush()
    await media_gc.release_urls(session, old_media)
    return True


async def publish_post(session: AsyncSession, post_id: str) -> dict | None:
    """发布 = 同步索引 + 置状态。正文为空 / 标题撞车时抛 ValueError（路由转 400）。"""
    row = await _get_row(session, post_id)
    if row is None:
        return None
    await _sync_index(session, row)
    if row.status != STATUS_PUBLISHED:
        row.published_at = row.published_at or time.time()
    row.status = STATUS_PUBLISHED
    row.updated_at = time.time()
    await session.flush()
    return await _with_category(session, _clean_row(row))


async def unpublish_post(session: AsyncSession, post_id: str) -> dict | None:
    """下线 = 从知识库摘掉索引，回到草稿状态（published_at 保留首次发布时间）。"""
    row = await _get_row(session, post_id)
    if row is None:
        return None
    await _remove_index(session, row)
    row.status = STATUS_DRAFT
    row.updated_at = time.time()
    await session.flush()
    return await _with_category(session, _clean_row(row))


# --- 前台公开查询 ---


async def list_published(
    session: AsyncSession,
    *,
    tag: str | None = None,
    category: str | None = None,
    featured: bool | None = None,
    offset: int = 0,
    limit: int = 20,
) -> dict:
    """首页 / 标签页 / 分类页列表：只有已发布文章，按发布时间倒序。

    ``category`` 收前台 slug；slug 不存在时直接给空页（不给 404，
    避免分类页乱链把前台打成错误页）。
    """
    stmt = select(BlogPost).where(BlogPost.status == STATUS_PUBLISHED)
    if featured:
        stmt = stmt.where(BlogPost.featured.is_(True))
    cat_id: str | None = None
    if category:
        cat = await session.scalar(
            select(BlogCategory).where(BlogCategory.slug == category)
        )
        if cat is None:
            return {"total": 0, "offset": offset, "limit": limit, "items": []}
        cat_id = cat.id
        stmt = stmt.where(BlogPost.category_id == cat_id)
    if tag:
        rows = (await session.scalars(stmt.order_by(desc(BlogPost.published_at)))).all()
        rows = [r for r in rows if tag in (r.tags or [])]
        page = rows[offset : offset + limit]
        items = await _with_category_list(session, [_public_item(r) for r in page], public=True)
        return {
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "items": items,
        }
    total = (
        await session.execute(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        )
    ).scalar_one()
    rows = (
        await session.scalars(
            stmt.order_by(desc(BlogPost.published_at)).offset(offset).limit(limit)
        )
    ).all()
    items = await _with_category_list(session, [_public_item(r) for r in rows], public=True)
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": items,
    }


def _public_item(row: BlogPost) -> dict:
    """前台项：去掉内部 ID / provider 等运维字段。

    ``category_id`` 故意留到这里：由 _attach_category(public=True) 换成
    展示摘要后抹掉。
    """
    data = _clean_row(row)
    for key in ("id", "status", "knowledge_id"):
        data.pop(key)
    return data


async def get_published_by_slug(session: AsyncSession, slug: str) -> dict | None:
    """前台文章页：按 slug 取正文；访问成功（含重复）计一次阅读数。"""
    row = await session.scalar(
        select(BlogPost).where(BlogPost.slug == slug, BlogPost.status == STATUS_PUBLISHED)
    )
    if row is None:
        return None
    row.view_count = int(row.view_count or 0) + 1
    await session.flush()
    data = _public_item(row)
    data["content"] = row.content or ""
    data["updated_at"] = row.updated_at
    return await _with_category(session, data, public=True)


async def post_neighbors(session: AsyncSession, slug: str) -> dict | None:
    """上一篇 / 下一篇（按发布时间正序；同刻用 ID 稳定排序）。

    文章不存在 / 未发布返回 None。不计 view_count——导航接口不该污染阅读数。
    个人博客量级小，直接全量拉已发布文章在 Python 里定位，
    与标签筛选同一套策略，避开 published_at 为 NULL 时的 SQL 比较盲区。
    """
    rows = (
        await session.scalars(
            select(BlogPost).where(BlogPost.status == STATUS_PUBLISHED)
        )
    ).all()
    ordered = sorted(rows, key=lambda r: (r.published_at or r.created_at or 0, r.id))
    anchor = next((i for i, r in enumerate(ordered) if r.slug == slug), None)
    if anchor is None:
        return None

    def _brief(row: BlogPost | None) -> dict | None:
        return {"slug": row.slug, "title": row.title} if row else None

    return {
        "prev": _brief(ordered[anchor - 1] if anchor > 0 else None),
        "next": _brief(ordered[anchor + 1] if anchor < len(ordered) - 1 else None),
    }


async def related_posts(
    session: AsyncSession, slug: str, *, limit: int = 5
) -> list[dict] | None:
    """相关文章推荐：同分类 +2 分、每命中一个共同标签 +1 分，取前 N 篇。

    基准文章不存在 / 未发布返回 None；没有任何命中给空列表。
    """
    base = await session.scalar(
        select(BlogPost).where(BlogPost.slug == slug, BlogPost.status == STATUS_PUBLISHED)
    )
    if base is None:
        return None
    rows = (
        await session.scalars(
            select(BlogPost).where(
                BlogPost.status == STATUS_PUBLISHED, BlogPost.id != base.id
            )
        )
    ).all()
    my_tags = {t for t in (base.tags or []) if isinstance(t, str)}
    scored: list[tuple[int, float, dict]] = []
    for r in rows:
        score = 0
        if base.category_id and r.category_id == base.category_id:
            score += 2
        score += len(my_tags & {t for t in (r.tags or []) if isinstance(t, str)})
        if score > 0:
            scored.append((score, r.published_at or 0, _public_item(r)))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    picks = [item for _, _, item in scored[:limit]]
    return await _with_category_list(session, picks, public=True)


async def tag_counts(session: AsyncSession) -> list[BlogTagItem]:
    """标签云：聚合已发布文章的 tags（按使用次数倒序，同次数按名称）。"""
    rows = (
        await session.scalars(
            select(BlogPost.tags).where(BlogPost.status == STATUS_PUBLISHED)
        )
    ).all()
    counts: dict[str, int] = {}
    for tags in rows:
        for tag in tags or []:
            if isinstance(tag, str) and tag:
                counts[tag] = counts.get(tag, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [BlogTagItem(name=name, count=count) for name, count in ordered]


# --- AI：语义搜索 / 问答 / 对话 ---


async def _published_docs(session: AsyncSession) -> dict[str, BlogPost]:
    """已发布且已同步索引的「知识文档 ID → 文章」映射（检索的限定范围）。"""
    rows = (
        await session.scalars(
            select(BlogPost).where(
                BlogPost.status == STATUS_PUBLISHED,
                BlogPost.knowledge_id.is_not(None),
            )
        )
    ).all()
    return {r.knowledge_id: r for r in rows if r.knowledge_id}


async def search_posts(
    session: AsyncSession, req: BlogSearchRequest
) -> BlogSearchResponse:
    """博客语义搜索：混合检索命中分片后塌缩到文章（同文多片只留最高一条）。"""
    docs = await _published_docs(session)
    if not docs:
        return BlogSearchResponse(query=req.query, total=0, items=[])
    resp = await rag.search(
        session,
        KnowledgeSearchRequest(
            query=req.query, top_k=min(req.top_k * CHUNKS_PER_POST, 20)
        ),
        restrict_doc_ids=list(docs),
    )
    best: dict[str, BlogCitation] = {}
    for cit in resp.items:
        post = docs.get(cit.knowledge_id)
        if post is None:
            continue
        if cit.knowledge_id in best:
            continue  # 结果已按得分序，第一篇即该文的最高分片段
        best[cit.knowledge_id] = BlogCitation(
            index=0,  # 塌缩到文章后统一重排（见下）
            slug=post.slug,
            title=post.title,
            score=round(float(cit.score), 4),
            snippet=cit.snippet,
        )
    items = list(best.values())[: req.top_k]
    # 角标从 1 开始：与模型 Context 里的 [n] 和前台展示保持同一口径
    for i, item in enumerate(items):
        item.index = i + 1
    return BlogSearchResponse(query=req.query, total=len(items), items=items)


def _context_block(citations: Sequence[BlogCitation]) -> str:
    """把命中片段拼成给模型的 Context（编号与引用角标一致）。"""
    return "\n".join(f"[{c.index}] {c.snippet}" for c in citations)


async def answer_question(
    session: AsyncSession, req: BlogSearchRequest
) -> BlogAnswerResponse:
    """一次性 AI 问答：检索 → 拼 Context → 对话模型。无命中时不调模型直接说明。"""
    hits = await search_posts(session, BlogSearchRequest(query=req.query, top_k=req.top_k))
    if not hits.items:
        return BlogAnswerResponse(
            query=req.query,
            answer="博客中未找到与这个问题相关的文章。",
            citations=[],
        )
    backend = llm.get_llm(None, feature="rag")
    question = (
        f"Context:\n{_context_block(hits.items)}\n\nQuestion: {req.query}"
    )
    content, _finish, _usage, _model = await backend.chat(
        [
            ChatMessage(role="system", content=BLOG_CHAT_SYSTEM_PROMPT),
            ChatMessage(role="user", content=question),
        ]
    )
    return BlogAnswerResponse(query=req.query, answer=content, citations=hits.items)


async def prepare_chat(
    session: AsyncSession, req: BlogChatRequest
) -> tuple[list[BlogCitation], list[ChatMessage]]:
    """组装博客对话的模型消息（**在返回流之前**把带 DB 的活全干完）。

    流式生成器会活过请求级 session，所以检索必须在这里做完；生成器里
    只剩「调模型 + 转发块」。历史按 ``CHAT_HISTORY_LIMIT`` 截断回喂，
    检索只对最后一条用户提问做。
    """
    last = str(req.messages[-1].content)
    hits = await search_posts(session, BlogSearchRequest(query=last, top_k=5))
    context = _context_block(hits.items) if hits.items else "（没有检索到相关文章）"
    history = [m for m in req.messages if m.role != "system"]
    if len(history) > CHAT_HISTORY_LIMIT:
        # 保首尾：最后一条是本轮提问，开头若干条保留话题上下文
        history = history[-(CHAT_HISTORY_LIMIT - 1) :]
    system = ChatMessage(
        role="system",
        content=(
            f"{BLOG_CHAT_SYSTEM_PROMPT}\n\nContext:\n{context}"
        ),
    )
    messages = [system, *history]
    return hits.items, messages


__all__: Sequence[str] = [
    "STATUS_DRAFT",
    "STATUS_PUBLISHED",
    "answer_question",
    "create_category",
    "create_post",
    "delete_category",
    "delete_post",
    "get_post",
    "get_published_by_slug",
    "list_categories",
    "list_posts",
    "list_published",
    "prepare_chat",
    "post_neighbors",
    "publish_post",
    "related_posts",
    "search_posts",
    "tag_counts",
    "unpublish_post",
    "update_category",
    "update_post",
]
