"""请求 / 响应模型（pydantic）：只描述数据形状，不写业务规则。

这里只默认导出一部分常用类型（user / admin / 信封）；路由直接从具体子模块
（``schemas.ai`` / ``schemas.upload`` 等）引入自己要的类型。
"""

from .admin import (
    AdminLoginRequest,
    AdminLoginResponse,
    AdminToken,
    AdminUserOut,
)
from .envelope import ApiResponse
from .user import User, UserCreate, UserUpdate

__all__ = [
    "User",
    "UserCreate",
    "UserUpdate",
    "AdminLoginRequest",
    "AdminLoginResponse",
    "AdminToken",
    "AdminUserOut",
    "ApiResponse",
]
