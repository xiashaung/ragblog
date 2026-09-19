"""数据分析（Text-to-SQL 智能问数）接口。

全部走 RBAC 权限 ``analytics:read``，并受总开关 ``APP_ANALYTICS_ENABLED`` 控制。
所有查询均为只读：生成的 SQL 经服务层强校验 + 只读事务兜底，凭证表 / 凭证列不外泄。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from .. import deps, services
from ..config import get_settings
from ..db import get_db
from ..rbac import Permission
from ..schemas.analytics import (
    AnalyticsHistoryResponse,
    AnalyticsQueryRequest,
    AnalyticsQueryResponse,
    AnalyticsSchemaResponse,
    AnalyticsSource,
)
from ..services.rbac_service import Principal

router = APIRouter()


def _ensure_enabled() -> None:
    if not get_settings().analytics_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "智能问数功能未启用（APP_ANALYTICS_ENABLED=false）"
        )


@router.get("/sources", response_model=list[AnalyticsSource])
async def list_sources(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.ANALYTICS_READ)),
) -> list[AnalyticsSource]:
    """问数页可选的数据源（内置主库 + 已启用的外部源），不含任何连接信息。"""
    _ensure_enabled()
    return await services.datasource.selectable_sources(session)


@router.get("/schema", response_model=AnalyticsSchemaResponse)
async def get_schema(
    source: str = "main",
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.ANALYTICS_READ)),
) -> AnalyticsSchemaResponse:
    """返回指定数据源可分析的表结构（已剔除系统表与凭证列），供前端展示与引导提问。"""
    _ensure_enabled()
    return await services.analytics.fetch_schema(session, source)


@router.post("/query", response_model=AnalyticsQueryResponse)
async def run_query(
    req: AnalyticsQueryRequest,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.ANALYTICS_READ)),
) -> AnalyticsQueryResponse:
    """自然语言问数：生成只读 SQL →（dry_run 可只出 SQL）→ 执行 → AI 结论。

    结果（含失败的友好报错）会落到本人的历史里，支撑对话式回放。
    """
    _ensure_enabled()
    resp = await services.analytics.query(session, req)
    await services.analytics.save_chat(session, principal.admin_id, resp)
    return resp


@router.get("/history", response_model=AnalyticsHistoryResponse)
async def get_history(
    limit: int = 100,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.ANALYTICS_READ)),
) -> AnalyticsHistoryResponse:
    """本人的问数历史（时间正序，最多 200 条），供对话流回放。"""
    _ensure_enabled()
    return await services.analytics.list_history(
        session, principal.admin_id, limit=max(1, min(limit, 200))
    )


@router.delete("/history/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_history_item(
    chat_id: str,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.ANALYTICS_READ)),
) -> None:
    """删除单条问答（仅本人的；不存在也返回成功，前端本地状态即真相）。"""
    _ensure_enabled()
    await services.analytics.delete_chat(session, principal.admin_id, chat_id)


@router.delete("/history", status_code=status.HTTP_204_NO_CONTENT)
async def clear_history(
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.ANALYTICS_READ)),
) -> None:
    """清空本人全部问数历史（不影响外部数据源本身）。"""
    _ensure_enabled()
    await services.analytics.clear_history(session, principal.admin_id)
