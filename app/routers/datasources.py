"""外部数据源管理接口（挂在后台管理面 /admin/data-sources）。

读需 ``datasource:read``；写 / 删 / 测试需 ``datasource:manage``（默认只给超级管理员）。
所有响应只回掩码连接串，绝不回传明文口令。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from .. import deps, services
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

router = APIRouter(prefix="/admin/data-sources", tags=["admin-datasource"])


@router.get("", response_model=list[DataSourceOut])
async def list_data_sources(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.DATASOURCE_READ)),
) -> list[DataSourceOut]:
    return await services.datasource.list_sources(session)


@router.post("", response_model=DataSourceOut, status_code=status.HTTP_201_CREATED)
async def create_data_source(
    payload: DataSourceCreate,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(
        deps.require_permission(Permission.DATASOURCE_MANAGE)
    ),
) -> DataSourceOut:
    row = await services.datasource.create(
        session,
        name=payload.name,
        dialect=payload.dialect,
        url=payload.url,
        readonly=payload.readonly,
        enabled=payload.enabled,
        description=payload.description,
    )
    return services.datasource.to_out(row)


@router.patch("/{source_id}", response_model=DataSourceOut)
async def update_data_source(
    source_id: str,
    payload: DataSourceUpdate,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(
        deps.require_permission(Permission.DATASOURCE_MANAGE)
    ),
) -> DataSourceOut:
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
    return services.datasource.to_out(row)


@router.delete("/{source_id}")
async def delete_data_source(
    source_id: str,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(
        deps.require_permission(Permission.DATASOURCE_MANAGE)
    ),
) -> dict:
    await services.datasource.delete(session, source_id)
    return {"deleted": True}


@router.post("/test", response_model=DataSourceTestResult)
async def test_data_source(
    payload: DataSourceTestRequest,
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(
        deps.require_permission(Permission.DATASOURCE_MANAGE)
    ),
) -> DataSourceTestResult:
    return await services.datasource.test_connection(
        session,
        dialect=payload.dialect,
        url=payload.url,
        existing_id=payload.existing_id,
    )
