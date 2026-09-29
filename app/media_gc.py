"""孤儿媒体文件回收：数据库不再引用某个上传文件时，把磁盘上的它也删掉。

背景：``/api/v1/upload`` 的图片落盘 ``media/uploads/<uuid>.<ext>``，数据库只存
URL。此前全项目没有任何一处会删文件——删文档、删文章、换封面、编辑器删图，
磁盘上的字节永远留下（目录只涨不消）。

安全口径（宁可漏删，不可错删）：

- 只认 ``<media_url>/uploads/`` 前缀下的 URL，解析后仍要在 uploads 真实目录内
  （挡掉 ``..`` 路径穿越）；外链、其它前缀一概忽略。
- 删前全库复查引用：知识文档 ``metadata``（含 images/embedded 落库的 URL）、
  博客正文 markdown 图片语法、封面列。任何一行还引用着就不动。
- 「先提交数据库改动，再调本模块」是调用方的义务：复查跑在同一个 session 上，
  flush 之后被删的行才不会再命中自己。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import Text, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import BlogPost, KnowledgeDoc

logger = logging.getLogger(__name__)

# markdown 图片语法 ![alt](url)：只取 url，属性尾巴（"title" 等）不属正则范围
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")


def extract_markdown_images(markdown: str | None) -> list[str]:
    """从 markdown 正文里抠出全部图片 URL（含重复，调用方去重）。"""
    if not markdown:
        return []
    return _MD_IMAGE_RE.findall(markdown)


def url_to_path(url: str) -> Path | None:
    """把 ``/media/uploads/x.png`` 解析成真实路径；不可回收的形态返 None。

    查询串/锚点会被剥掉（同一文件不同签名仍是同一字节）。
    """
    settings = get_settings()
    prefix = settings.media_url.rstrip("/") + "/uploads/"
    if not url.startswith(prefix):
        return None
    name = url[len(prefix) :].split("?")[0].split("#")[0]
    if not name or "/" in name or "\\" in name:
        return None  # 只收 uploads 直属的扁平文件名
    uploads = (settings.media_root / "uploads").resolve()
    path = (uploads / name).resolve()
    if not path.is_relative_to(uploads):
        return None  # 解析后逃出目录（理论上 "/" 已挡，纵深防御）
    return path


async def _still_referenced(session: AsyncSession, url: str) -> bool:
    """全库问一遍：还有没有哪一行引用这个 URL（JSON 列 cast 成文本做 LIKE）。"""
    like = f"%{url}%"
    doc = await session.execute(
        select(KnowledgeDoc.id).where(cast(KnowledgeDoc.meta, Text).like(like)).limit(1)
    )
    if doc.first() is not None:
        return True
    post = await session.execute(
        select(BlogPost.id)
        .where(or_(BlogPost.content.like(like), BlogPost.cover_url.like(like)))
        .limit(1)
    )
    return post.first() is not None


async def release_urls(session: AsyncSession, urls: Iterable[str]) -> int:
    """删掉「本会话已提交改动后不再被任何行引用」的文件，返删除数。

    调用时机：数据库删除/更新 **flush 之后**。文件删不掉（权限等）只记警告，
    不影响业务事务——孤儿文件宁可留给 sweep 兜底。
    """
    deleted = 0
    for url in sorted({u for u in urls if isinstance(u, str)}):
        path = url_to_path(url)
        if path is None or not path.exists():
            continue
        if await _still_referenced(session, url):
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            logger.warning("回收媒体文件失败 %s：%s", path, exc)
    return deleted


def doc_image_urls(meta: dict | None) -> list[str]:
    """知识文档 metadata 里落库的图片 URL 列表（kind=image 与内嵌图共用键）。"""
    images = (meta or {}).get("images")
    return list(images) if isinstance(images, list) else []


async def sweep_orphans(session: AsyncSession, *, grace_seconds: float = 3600.0) -> dict:
    """全目录兜底：逐个文件查引用，无人引用即删。给管理端点/巡检用。

    ``grace_seconds`` 是新建文件的宽限期（默认 1 小时）：Markdown 编辑器的
    图是先上传落盘、保存正文后才进数据库引用——刚传完还没保存的文件此刻
    就是「零引用」，不留宽限期会把编辑中的图误删。
    """
    settings = get_settings()
    uploads = (settings.media_root / "uploads").resolve()
    prefix = settings.media_url.rstrip("/") + "/uploads/"
    deadline = time.time() - grace_seconds
    scanned = deleted = kept = 0
    if uploads.is_dir():
        for path in sorted(uploads.iterdir()):
            if not path.is_file():
                continue
            scanned += 1
            if path.stat().st_mtime > deadline:
                kept += 1  # 太新：可能在编辑流程里，不碰
                continue
            if await _still_referenced(session, f"{prefix}{path.name}"):
                kept += 1
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                logger.warning("回收媒体文件失败 %s：%s", path, exc)
                kept += 1
    return {"scanned": scanned, "deleted": deleted, "kept": kept}
