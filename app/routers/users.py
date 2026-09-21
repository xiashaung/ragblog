"""用户（user）CRUD 路由：异步，共用 ``get_db`` 拿到的会话。

与 items 一样只是示例级的薄路由：参数交给 pydantic，规则交给 ``user_service``，
本层只把 ``None``/``False`` 翻译成 404。
"""

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
    """新建用户，返 201 + 完整对象。"""
    return await services.user_service.create_user(session, payload)


@router.get("", response_model=list[User])
async def list_users(session: AsyncSession = Depends(get_db)) -> list[User]:
    """列出全量用户（示例接口，没分页）。"""
    return await services.user_service.list_users(session)


@router.get("/{user_id}", response_model=User)
async def get_user(
    user_id: int, session: AsyncSession = Depends(get_db)
) -> User:
    """按 id 取一个用户，不存在返 404。"""
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
    """部分更新：只改传了值的字段，用户不存在返 404。"""
    user = await services.user_service.update_user(session, user_id, payload)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int, session: AsyncSession = Depends(get_db)
) -> None:
    """删用户：没删到行就当「不存在」，返 204 或 404。"""
    ok = await services.user_service.delete_user(session, user_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
