"""外部数据源管理接口（挂在后台管理面 /admin/data-sources）。

读需 ``datasource:read``；写 / 删 / 测试需 ``datasource:manage``（默认只给超级管理员）。
所有响应只回掩码连接串，绝不回传明文口令。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, deps, services
from ..db import get_db
from ..rbac import Permission
from ..schemas.datasource import (
    DataSourceCreate,
    DataSourceOut,
    DataSourceTestRequest,
    DataSourceTestResult,
    DataSourceUpdate,
)
from ..services.rbac_service import Principal

router = APIRouter(prefix="/api/v1/admin/data-sources", tags=["admin-datasource"])


@router.get("", response_model=list[DataSourceOut])
async def list_data_sources(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.DATASOURCE_READ)),
) -> list[DataSourceOut]:
    """列出全部数据源（含已禁用的），连接串只给掩码。"""
    return await services.datasource.list_sources(session)


@router.post("", response_model=DataSourceOut, status_code=status.HTTP_201_CREATED)
async def create_data_source(
    payload: DataSourceCreate,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(
        deps.require_permission(Permission.DATASOURCE_MANAGE)
    ),
) -> DataSourceOut:
    """新建数据源：名称唯一性与方言白名单由服务层把关，业务失败一律转 400。"""
    row = await services.datasource.create(
        session,
        name=payload.name,
        dialect=payload.dialect,
        url=payload.url,
        readonly=payload.readonly,
        enabled=payload.enabled,
        description=payload.description,
    )
    audit.event(
        audit.Actor.from_principal(principal), "datasource.create", row.name
    )
    return services.datasource.to_out(row)


@router.patch("/{source_id}", response_model=DataSourceOut)
async def update_data_source(
    source_id: str,
    payload: DataSourceUpdate,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(
        deps.require_permission(Permission.DATASOURCE_MANAGE)
    ),
) -> DataSourceOut:
    """部分更新：改过连接串 / 方言后会释放旧连接池，下次用时重建。"""
    row = await services.datasource.update(
        session,
        source_id,
        name=payload.name,
        dialect=payload.dialect,
        url=payload.url,
        readonly=payload.readonly,
        enabled=payload.enabled,
        description=payload.description,
    )
    # 只记改了哪些字段（如 url 会改动口令，但字段名不含值）
    audit.event(
        audit.Actor.from_principal(principal),
        "datasource.update",
        row.name,
        changed=sorted(payload.model_dump(exclude_unset=True)),
    )
    return services.datasource.to_out(row)


@router.delete("/{source_id}")
async def delete_data_source(
    source_id: str,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(
        deps.require_permission(Permission.DATASOURCE_MANAGE)
    ),
) -> dict:
    """删数据源（连带释放它的连接池）；id 不存在由服务层抛 400。"""
    await services.datasource.delete(session, source_id)
    audit.event(audit.Actor.from_principal(principal), "datasource.delete", source_id)
    return {"deleted": True}


@router.post("/test", response_model=DataSourceTestResult)
async def test_data_source(
    payload: DataSourceTestRequest,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(
        deps.require_permission(Permission.DATASOURCE_MANAGE)
    ),
) -> DataSourceTestResult:
    """连通性测试：拿没保存的表单也能试；编辑态下 url 留空则沿用库里已存的。"""
    return await services.datasource.test_connection(
        session,
        dialect=payload.dialect,
        url=payload.url,
        existing_id=payload.existing_id,
    )
