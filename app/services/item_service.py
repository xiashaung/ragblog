"""items 表的异步仓库（示例业务数据，口径与 user_service 一致）。

会话由路由的 ``get_db`` 持有，本层只 ``flush`` 不 ``commit``；没找到返
``None`` / ``False``，由路由翻译成 404。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ItemRow
from ..schemas.item import Item, ItemCreate


def _to_schema(row: ItemRow) -> Item:
    """ORM 行 → 对外模型。"""
    return Item(
        id=row.id,
        title=row.title,
        description=row.description,
        price=row.price,
        owner_id=row.owner_id,
    )


async def create_item(session: AsyncSession, data: ItemCreate) -> Item:
    """新建条目；``owner_id`` 固定留空（示例接口不参与归属校验）。"""
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
    """按 id 取一条，不存在返 ``None``。"""
    row = await session.get(ItemRow, item_id)
    return _to_schema(row) if row else None


async def list_items(session: AsyncSession) -> list[Item]:
    """列出全量条目（示例接口，没做分页）。"""
    result = await session.scalars(select(ItemRow).order_by(ItemRow.id))
    return [_to_schema(r) for r in result.all()]


async def delete_item(session: AsyncSession, item_id: int) -> bool:
    """删除条目，返是否真的删了。"""
    row = await session.get(ItemRow, item_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
