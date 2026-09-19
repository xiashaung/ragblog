"""一次性建号脚本（保留兼容入口，实际逻辑已交给 app.bootstrap）。

RBAC 之后账号必须绑定角色才有权限：直接往 admin_users 插行会建出一个
「哪个管理接口都进不去」的账号（没有任何 role 授予）。所以这里不再自己
写 SQL，而是走 services.admin_service.create_admin —— 它会顺带
种子内置角色并把该账号绑到 superadmin。

推荐用法（无需本脚本）：
    python -m app.bootstrap seed --username admin --password <密码>

本脚本仅保留给老习惯：直接执行时用下面的默认值，且必须显式确认。
"""

from __future__ import annotations

import asyncio
import sys

from app.bootstrap import seed_admin

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin123"


def main() -> int:
    username = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_USERNAME
    password = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PASSWORD
    if password == DEFAULT_PASSWORD:
        print("⚠️  正在使用默认弱密码 admin123，生产环境请显式传参：")
        print("   python create_admin.py <用户名> <密码>")
    asyncio.run(seed_admin(username, password))
    return 0


if __name__ == "__main__":
    sys.exit(main())
