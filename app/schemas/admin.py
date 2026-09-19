"""Admin dashboard request/response schemas.

Authentication is bearer-token based: login mints a signed session token
(the client carries it as ``Authorization: Bearer <token>``); logout and
token expiry invalidate it. The cookie is only a transport hint for the
browser so ``withCredentials`` is set on the axios instance.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1)


class AdminLoginResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_at: float
    admin: AdminUserOut
    # RBAC：登录后直接下发，前端据此渲染菜单与按钮
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class AdminUserOut(BaseModel):
    id: str
    username: str
    name: str = ""
    is_active: bool = True


class AdminCreate(BaseModel):
    """Bootstrapping schema to create the first admin account.

    Public — intentionally, but only the very first call succeeds (the
    service refuses to create an account when one already exists), so it
    acts as a self-guarding onboarding endpoint.
    """

    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=8)
    name: str = Field(default="", max_length=100)


class AdminToken(BaseModel):
    """A token + its expiry, used across login/refresh/logout."""

    token: str
    token_type: str = "bearer"
    expires_at: float


# --- RBAC ---


class PrincipalOut(BaseModel):
    """``/admin/me`` 返回：当前登录身份 + 实际生效的角色与权限。"""

    id: str
    username: str
    name: str = ""
    is_active: bool = True
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    is_superadmin: bool = False
    live_sessions: int = 0


class PermissionOut(BaseModel):
    """权限码目录（代码定义，供前端渲染勾选框）。"""

    code: str
    label: str
    group: str
    description: str = ""


class RoleOut(BaseModel):
    id: str
    code: str
    name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)
    is_builtin: bool = False
    members: int = 0


class RoleCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=50)
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=200)
    permissions: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    """所有字段可选；superadmin 的 permissions 会被服务层拒绝修改。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=200)
    permissions: list[str] | None = None


class AccountOut(BaseModel):
    id: str
    username: str
    name: str = ""
    is_active: bool = True
    created_at: float = 0.0
    roles: list[str] = Field(default_factory=list)


class AccountCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=8, description="至少 8 位")
    name: str = Field(default="", max_length=100)
    roles: list[str] = Field(default_factory=list, description="角色 code 列表")


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8)


class AccountRolesUpdate(BaseModel):
    """覆盖式分配角色：传 ``["admin"]`` 就是把账号角色重置为仅「管理员」。"""

    roles: list[str] = Field(default_factory=list)


__all__ = [
    "AccountCreate",
    "AccountOut",
    "AccountRolesUpdate",
    "AccountUpdate",
    "AdminCreate",
    "AdminLoginRequest",
    "AdminLoginResponse",
    "AdminToken",
    "AdminUserOut",
    "PermissionOut",
    "PrincipalOut",
    "RoleCreate",
    "RoleOut",
    "RoleUpdate",
]
