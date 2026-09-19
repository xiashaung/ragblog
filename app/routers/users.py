"""User CRUD routes (async, backed by the shared DB session)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from .. import services
from ..db import get_db
from ..schemas import User, UserCreate, UserUpdate

router = APIRouter()


@router.post("", response_model=User, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate, session: AsyncSession = Depends(get_db)
) -> User:
    return await services.user_service.create_user(session, payload)


@router.get("", response_model=list[User])
async def list_users(session: AsyncSession = Depends(get_db)) -> list[User]:
    return await services.user_service.list_users(session)


@router.get("/{user_id}", response_model=User)
async def get_user(
    user_id: int, session: AsyncSession = Depends(get_db)
) -> User:
    user = await services.user_service.get_user(session, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


@router.patch("/{user_id}", response_model=User)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    session: AsyncSession = Depends(get_db),
) -> User:
    user = await services.user_service.update_user(session, user_id, payload)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int, session: AsyncSession = Depends(get_db)
) -> None:
    ok = await services.user_service.delete_user(session, user_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
