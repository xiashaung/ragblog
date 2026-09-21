"""管理后台的一次性初始化脚本（不随应用启动跑）。

用法：
    python -m app.bootstrap seed --username admin --password secret

流程：建引擎 → 建表 → 创建第一个管理员（幂等：库里已有账号就跳过）。
创建的账号会绑到内置的 ``superadmin`` 角色 —— 手写一行 ``admin_users``
会得到一个没有任何权限码、登进后台也看不到菜单的空账号。

根目录的 ``create_admin.py`` 只是本模块的一层兼容入口（默认弱密码 + 不带参数），
新环境建议直接用上面的 ``python -m app.bootstrap seed`` 写法。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import services
from .config import get_settings
from .db import get_session_factory, init_db, init_models


async def seed_admin(username: str, password: str) -> None:
    """播种首个管理员；已有账号时只打一行提示、不报错退出。"""
    settings = get_settings()
    init_db(database_url=settings.database_url)
    await init_models()

    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            await services.admin_service.create_admin(
                session, username, password, name=username
            )
            print(f"Created admin account: {username}")
        except ValueError as exc:
            print(f"Skipped (accounts already exist): {exc}")


def main(argv: list[str] | None = None) -> int:
    """命令行入口：目前只有 ``seed`` 子命令，返 shell 退出码。"""
    parser = argparse.ArgumentParser(description="Admin dashboard bootstrap")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="Create the first admin account")
    seed.add_argument("--username", required=True)
    seed.add_argument("--password", required=True)

    args = parser.parse_args(argv)
    if args.command == "seed":
        asyncio.run(seed_admin(args.username, args.password))
        return 0
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
