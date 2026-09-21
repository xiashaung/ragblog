"""users 表的异步仓库（示例业务数据，不是登录账号）。

所有方法都接受一个 ``db.AsyncSession``：会话由路由通过 ``get_db`` 依赖拿到并持有，
本层只 ``flush``、不 ``commit``（提交在依赖的收尾里），这样多个服务能共用一个事务。
返回 None / False 表示「没找到」，由路由翻译成 404（服务层不抛 HTTP 状态）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import UserRow
from ..schemas import User, UserCreate, UserUpdate


def _to_schema(row: UserRow) -> User:
    """ORM 行 → 对外模型（不把行对象直接递出路由）。"""
    return User(
        id=row.id, name=row.name, email=row.email, is_active=row.is_active
    )


async def create_user(session: AsyncSession, data: UserCreate) -> User:
    """新建用户；flush 是为了拿到自增 id 好拼回响应。"""
    row = UserRow(name=data.name, email=data.email, is_active=True)
    session.add(row)
    await session.flush()  # 分配 row.id
    return _to_schema(row)


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    """按 id 取一个用户，不存在返 ``None``。"""
    row = await session.get(UserRow, user_id)
    return _to_schema(row) if row else None


async def list_users(session: AsyncSession) -> list[User]:
    """列出全量用户（示例接口，没做分页）。"""
    result = await session.scalars(select(UserRow).order_by(UserRow.id))
    return [_to_schema(r) for r in result.all()]


async def update_user(
    session: AsyncSession, user_id: int, data: UserUpdate
) -> User | None:
    """部分更新：只改传了值的字段（``None`` 表示「没动」，不是「清空」）。"""
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
    """删除用户，返是否真的删了（不存在不算错）。"""
    row = await session.get(UserRow, user_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
