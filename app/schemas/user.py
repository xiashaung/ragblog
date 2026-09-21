"""用户（user）示例接口的模型。

``UserUpdate`` 全字段可选，才能表达「只改传了值的那个字段」；其余类只靠
继承组合出创建 / 对外两个形状。
"""

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserBase(BaseModel):
    """写与读共用的字段；邮箱格式在 pydantic 层就校验。"""

    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr


class UserCreate(UserBase):
    """新建用户的入参（字段与 ``UserBase`` 完全一致）。"""

    pass


class UserUpdate(BaseModel):
    """部分更新：``None`` 表示「这条没动」，不是「把它清空」。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    email: EmailStr | None = None


class User(UserBase):
    """对外的用户：额外带主键与启用位，不含任何凭据字段。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool = True
