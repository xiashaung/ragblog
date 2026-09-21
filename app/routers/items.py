"""条目（item）CRUD 路由：异步，共用 ``get_db`` 拿到的会话。

只做「收参 → 调 ``item_service`` → 把 ``None``/``False`` 翻译成 404」，
不写业务规则；``detail`` 会被异常处理器直接透成信封里的 ``message``。
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from .. import services
from ..db import get_db
from ..schemas.item import Item, ItemCreate

router = APIRouter()


@router.post("", response_model=Item, status_code=status.HTTP_201_CREATED)
async def create_item(
    payload: ItemCreate, session: AsyncSession = Depends(get_db)
) -> Item:
    """新建条目，返 201 + 完整对象。"""
    return await services.item_service.create_item(session, payload)


@router.get("", response_model=list[Item])
async def list_items(session: AsyncSession = Depends(get_db)) -> list[Item]:
    """列出全量条目（示例接口，没分页）。"""
    return await services.item_service.list_items(session)


@router.get("/{item_id}", response_model=Item)
async def get_item(
    item_id: int, session: AsyncSession = Depends(get_db)
) -> Item:
    """按 id 取一条，不存在返 404。"""
    item = await services.item_service.get_item(session, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    return item


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: int, session: AsyncSession = Depends(get_db)
) -> None:
    """删一条；没删到任何行就当「不存在」，返 204 或 404。"""
    ok = await services.item_service.delete_item(session, item_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
