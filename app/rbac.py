"""RBAC 权限目录与内置角色（代码是权限码的唯一来源）。

设计取舍：
- **权限码写在代码里**（``Permission``），角色存在数据库里。角色可以随时增删改、
  勾配权限，但「一个权限码代表什么能力」必须由代码定义 —— 否则路由上的
  ``require_permission("kb:write")`` 会和数据库里被人随手改动的字符串对不上。
- ``SUPERADMIN_ROLE`` 用通配 ``*`` 表示全权，不逐条列举，避免加新权限码时漏配。
- 内置角色（``BUILTIN_ROLES``）在迁移里落库；``is_builtin`` 的角色不允许删、
  不允许改 code，但**权限可以调整**（除了 superadmin，它恒为全权）。
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    """可授予的能力。命名规范 ``资源:动作``。"""

    ACCOUNT_READ = "account:read"
    ACCOUNT_MANAGE = "account:manage"
    ROLE_READ = "role:read"
    ROLE_MANAGE = "role:manage"
    SESSION_READ = "session:read"
    SESSION_REVOKE = "session:revoke"
    ANALYTICS_READ = "analytics:read"
    DATASOURCE_READ = "datasource:read"
    DATASOURCE_MANAGE = "datasource:manage"


ALL_PERMISSIONS: list[str] = [p.value for p in Permission]

#: 前端渲染分组用的目录（code / 名称 / 说明）
PERMISSION_CATALOG: list[dict[str, str]] = [
    {
        "code": Permission.ACCOUNT_READ,
        "label": "查看账号",
        "group": "账号管理",
        "description": "列出后台账号及其角色",
    },
    {
        "code": Permission.ACCOUNT_MANAGE,
        "label": "管理账号",
        "group": "账号管理",
        "description": "新建 / 改名 / 停用 / 重置密码 / 删除账号，并为其分配角色",
    },
    {
        "code": Permission.ROLE_READ,
        "label": "查看角色",
        "group": "角色权限",
        "description": "列出角色与各自权限",
    },
    {
        "code": Permission.ROLE_MANAGE,
        "label": "管理角色",
        "group": "角色权限",
        "description": "新建 / 修改 / 删除角色并配置权限（提权相关，默认只给超级管理员）",
    },
    {
        "code": Permission.SESSION_READ,
        "label": "查看会话",
        "group": "会话",
        "description": "查看在线登录会话",
    },
    {
        "code": Permission.SESSION_REVOKE,
        "label": "吊销会话",
        "group": "会话",
        "description": "强制下线某个会话",
    },
    {
        "code": Permission.ANALYTICS_READ,
        "label": "智能问数",
        "group": "数据分析",
        "description": "连接数据库做只读 Text-to-SQL 分析（自动生成并执行 SELECT，不碰凭证表）",
    },
    {
        "code": Permission.DATASOURCE_READ,
        "label": "查看数据源",
        "group": "数据分析",
        "description": "查看可切换的外部数据源列表（连接串只回掩码）",
    },
    {
        "code": Permission.DATASOURCE_MANAGE,
        "label": "管理数据源",
        "group": "数据分析",
        "description": "新建 / 修改 / 删除外部数据源（含连接凭证，高敏感）；默认只给超级管理员",
    },
]

SUPERADMIN_ROLE = "superadmin"
WILDCARD = "*"

#: 迁移与 seed 共用的内置角色定义。
#: admin 有意不含 ``role:manage``：能管账号但不能改角色，避免自我提权。
BUILTIN_ROLES: list[dict[str, object]] = [
    {
        "code": SUPERADMIN_ROLE,
        "name": "超级管理员",
        "description": "拥有全部权限，不可删除、不可降权",
        "permissions": [WILDCARD],
    },
    {
        "code": "admin",
        "name": "管理员",
        "description": "管账号与会话，但不能改角色权限（防自我提权）",
        "permissions": [
            Permission.ACCOUNT_READ,
            Permission.ACCOUNT_MANAGE,
            Permission.ROLE_READ,
            Permission.SESSION_READ,
            Permission.SESSION_REVOKE,
            Permission.ANALYTICS_READ,
            Permission.DATASOURCE_READ,
        ],
    },
    {
        "code": "operator",
        "name": "运维",
        "description": "负责会话，不碰账号和角色",
        "permissions": [
            Permission.SESSION_READ,
            Permission.SESSION_REVOKE,
        ],
    },
    {
        "code": "viewer",
        "name": "只读",
        "description": "只能查看，不能改动任何配置",
        "permissions": [
            Permission.ACCOUNT_READ,
            Permission.ROLE_READ,
            Permission.SESSION_READ,
        ],
    },
]


def is_valid_permission(code: str) -> bool:
    """权限码是否由本模块定义（写库前校验，防脏数据）。"""
    return code in ALL_PERMISSIONS


def filter_valid_permissions(codes: object) -> list[str]:
    """挑出合法权限码并按目录顺序去重；``*`` 只在 superadmin 语义里出现。"""
    if not isinstance(codes, (list, tuple, set)):
        return []
    wanted = {code for code in codes if isinstance(code, str)}
    if WILDCARD in wanted:
        return [WILDCARD]
    return [code for code in ALL_PERMISSIONS if code in wanted]


def grants(permissions: list[str] | set[str], required: str) -> bool:
    """单个权限判断：通配 ``*`` 覆盖一切。"""
    return WILDCARD in permissions or required in permissions


__all__ = [
    "ALL_PERMISSIONS",
    "BUILTIN_ROLES",
    "PERMISSION_CATALOG",
    "Permission",
    "SUPERADMIN_ROLE",
    "WILDCARD",
    "filter_valid_permissions",
    "grants",
    "is_valid_permission",
]
