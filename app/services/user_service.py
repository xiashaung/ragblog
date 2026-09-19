"""Async user repository backed by the shared session.

All methods take a ``db.AsyncSession`` — the session is owned by the
router via the ``get_db`` dependency.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import UserRow
from ..schemas import User, UserCreate, UserUpdate


def _to_schema(row: UserRow) -> User:
    return User(
        id=row.id, name=row.name, email=row.email, is_active=row.is_active
    )


async def create_user(session: AsyncSession, data: UserCreate) -> User:
    row = UserRow(name=data.name, email=data.email, is_active=True)
    session.add(row)
    await session.flush()  # assigns row.id
    return _to_schema(row)


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    row = await session.get(UserRow, user_id)
    return _to_schema(row) if row else None


async def list_users(session: AsyncSession) -> list[User]:
    result = await session.scalars(select(UserRow).order_by(UserRow.id))
    return [_to_schema(r) for r in result.all()]


async def update_user(
    session: AsyncSession, user_id: int, data: UserUpdate
) -> User | None:
    row = await session.get(UserRow, user_id)
    if row is None:
        return None
    if data.name is not None:
        row.name = data.name
    if data.email is not None:
        row.email = data.email
    await session.flush()
    return _to_schema(row)


async def delete_user(session: AsyncSession, user_id: int) -> bool:
    row = await session.get(UserRow, user_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
