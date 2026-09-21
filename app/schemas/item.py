"""条目（item）示例接口的模型：入库字段与对外字段分开定义。

``ItemBase`` 放公共字段，``ItemCreate`` 直接继承（没额外字段），
``Item`` 多带 id / owner_id 并开了 ``from_attributes``，能直接从 ORM 行转。
"""

from pydantic import BaseModel, ConfigDict, Field


class ItemBase(BaseModel):
    """写与读共用的业务字段（标题非空、价格不为负）。"""

    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    price: float = Field(..., ge=0)


class ItemCreate(ItemBase):
    """新建条目的入参：字段与 ``ItemBase`` 完全一致。"""

    pass


class Item(ItemBase):
    """对外的条目：额外带主键与归属用户（``owner_id`` 当前恒为空）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int | None = None
