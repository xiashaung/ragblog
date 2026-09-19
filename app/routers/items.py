"""Item CRUD routes (async, backed by the shared DB session)."""

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
    return await services.item_service.create_item(session, payload)


@router.get("", response_model=list[Item])
async def list_items(session: AsyncSession = Depends(get_db)) -> list[Item]:
    return await services.item_service.list_items(session)


@router.get("/{item_id}", response_model=Item)
async def get_item(
    item_id: int, session: AsyncSession = Depends(get_db)
) -> Item:
    item = await services.item_service.get_item(session, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    return item


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: int, session: AsyncSession = Depends(get_db)
) -> None:
    ok = await services.item_service.delete_item(session, item_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
