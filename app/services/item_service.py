"""Async item repository backed by the shared session."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ItemRow
from ..schemas.item import Item, ItemCreate


def _to_schema(row: ItemRow) -> Item:
    return Item(
        id=row.id,
        title=row.title,
        description=row.description,
        price=row.price,
        owner_id=row.owner_id,
    )


async def create_item(session: AsyncSession, data: ItemCreate) -> Item:
    row = ItemRow(
        title=data.title,
        description=data.description,
        price=data.price,
        owner_id=None,
    )
    session.add(row)
    await session.flush()
    return _to_schema(row)


async def get_item(session: AsyncSession, item_id: int) -> Item | None:
    row = await session.get(ItemRow, item_id)
    return _to_schema(row) if row else None


async def list_items(session: AsyncSession) -> list[Item]:
    result = await session.scalars(select(ItemRow).order_by(ItemRow.id))
    return [_to_schema(r) for r in result.all()]


async def delete_item(session: AsyncSession, item_id: int) -> bool:
    row = await session.get(ItemRow, item_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
