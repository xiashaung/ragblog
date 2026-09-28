"""媒体运维路由：孤儿上传文件的全目录兜底清理。

删除路径（删文档/删文章/换图）已经在服务层挂了引用计数回收，本端点是给
「历史欠账与异常残留」兜底的：回收逻辑上线前的存量孤儿、落盘成功但事务
回滚留下的文件等。扫描 uploads 目录逐个查引用，无人引用即删。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from .. import deps, media_gc
from ..db import get_db
from ..rbac import Permission
from ..services.rbac_service import Principal

router = APIRouter(tags=["media"])


@router.post("/media/sweep")
async def sweep_orphan_media(
    grace_minutes: int = Query(default=60, ge=0, description="新建文件的宽限分钟数"),
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_MANAGE)),
) -> dict:
    """扫描 uploads 目录，删掉不再被任何知识文档/博客引用的文件。

    宽限期默认 60 分钟：编辑器刚传、正文还没保存的图不能被扫掉。
    """
    return await media_gc.sweep_orphans(session, grace_seconds=grace_minutes * 60.0)
