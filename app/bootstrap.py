"""One-time bootstrap helpers for the admin dashboard.

Usage:
    python -m app.bootstrap seed --username admin --password secret

Boots the DB engine, ensures tables exist, then creates the first admin
account (idempotent: it is a no-op if any admin already exists). The account
is bound to the builtin ``superadmin`` role -- creating a row in
``admin_users`` by hand would leave it without any permission.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import services
from .config import get_settings
from .db import get_session_factory, init_db, init_models


async def seed_admin(username: str, password: str) -> None:
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
