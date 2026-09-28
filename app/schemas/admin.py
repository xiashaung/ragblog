"""控制台（后台）的请求 / 响应模型。

认证基于 Bearer token：登录签一个会话 token（客户端以
``Authorization: Bearer <token>`` 带上），登出与到期都会让它失效。
同时下发的 ``admin_token`` cookie 只是给浏览器的一份携带副本，
所以 axios 实例要开 ``withCredentials``。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    """登录入参：账号口令（口令不出现在任何响应里）。"""

    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1)


class AdminLoginResponse(BaseModel):
    """登录结果：明文 token（只在这一刻出现一次）+ 账号与权限。"""

    token: str
    token_type: str = "bearer"
    expires_at: float
    admin: AdminUserOut
    # RBAC：登录后直接下发，前端据此渲染菜单与按钮
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class AdminUserOut(BaseModel):
    """对外披露的账号基本信息（不含口令与 token）。"""

    id: str
    username: str
    name: str = ""
    is_active: bool = True


class AdminCreate(BaseModel):
    """首次引导专用的建账号模型。

    确实是公开接口，但只有第一次能成（库里已有账号时服务层直接拒），
    所以它自带护栏，不会变成公开的注册入口。
    """

    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=8)
    name: str = Field(default="", max_length=100)


class AdminToken(BaseModel):
    """token 加它的到期时间（登录 / 刷新 / 会话相关接口复用）。"""

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
    """对外的角色：带权限码清单、是否内置，以及当前成员数。"""

    id: str
    code: str
    name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)
    is_builtin: bool = False
    members: int = 0


class RoleCreate(BaseModel):
    """新建角色：``code`` 是唯一标识（服务层会限制为小写字母/数字/下划线）。"""

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
    """对外的后台账号（带角色 code 列表，不带任何凭据）。"""

    id: str
    username: str
    name: str = ""
    is_active: bool = True
    created_at: float = 0.0
    roles: list[str] = Field(default_factory=list)


class AccountCreate(BaseModel):
    """新建后台账号（需 ``account:manage``）；角色按 code 传。"""

    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=8, description="至少 8 位")
    name: str = Field(default="", max_length=100)
    roles: list[str] = Field(default_factory=list, description="角色 code 列表")


class AccountUpdate(BaseModel):
    """改名字 / 启停 / 重置密码；``None`` 表示该项没动，停用与改密会踢掉会话。"""

    name: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8)


class AccountRolesUpdate(BaseModel):
    """覆盖式分配角色：传 ``["admin"]`` 就是把账号角色重置为仅「管理员」。"""

    roles: list[str] = Field(default_factory=list)


class PasswordChange(BaseModel):
    """自助改密：验旧口令、设新口令（两个口令都只进不出，不回显）。"""

    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, description="至少 8 位")


class ConfigItem(BaseModel):
    """系统配置项。"""

    key: str
    value: str
    description: str = ""


class ConfigListResponse(BaseModel):
    """配置列表响应。"""

    configs: list[ConfigItem] = Field(default_factory=list)


class ConfigUpdateRequest(BaseModel):
    """配置更新请求。"""

    value: str = Field(..., min_length=1, max_length=2000)


__all__ = [
    "AccountCreate",
    "AccountOut",
    "AccountRolesUpdate",
    "AccountUpdate",
    "ConfigItem",
    "ConfigListResponse",
    "ConfigUpdateRequest",
    "AdminCreate",
    "AdminLoginRequest",
    "AdminLoginResponse",
    "AdminToken",
    "AdminUserOut",
    "PasswordChange",
    "PermissionOut",
    "PrincipalOut",
    "RoleCreate",
    "RoleOut",
    "RoleUpdate",
]
