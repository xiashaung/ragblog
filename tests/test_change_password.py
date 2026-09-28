"""自助改密 ``POST /api/v1/admin/me/password`` 的行为测试。

核心诉求：只有普通权限（如 viewer）的账号也能改自己密码，不必求
``account:manage``；改完保留当前会话、踢掉其它设备；旧口令校验吃登录限流。
"""

from __future__ import annotations

import time
import uuid

from app import db
from app.models import AdminSession
from app.services.admin_service import _hash_token

NEW_PASSWORD = "brand-new-pass-123"


async def _seed_extra_session(admin_id: str) -> str:
    """直接给账号塞一条额外会话，模拟「另一台设备已登录」，返其明文 token。"""
    token = f"admin_otherdev-{uuid.uuid4().hex}"
    async with db.session_factory() as s:
        s.add(
            AdminSession(
                token_hash=_hash_token(token),
                admin_id=admin_id,
                expires_at=time.time() + 3600,
            )
        )
        await s.commit()
    return token


async def test_member_can_change_own_password(client, account_factory) -> None:
    """普通 viewer 账号（无 account:manage）改自己密码：新口令能登、旧口令失效。"""
    acct = await account_factory("selfsvc1", ["viewer"])
    old_password = "member-pass-123"
    res = await client.post(
        "/api/v1/admin/me/password",
        json={"old_password": old_password, "new_password": NEW_PASSWORD},
        headers=acct["headers"],
    )
    assert res.status_code == 200, res.text

    # 新口令可登录
    res = await client.post(
        "/api/v1/admin/login",
        json={"username": "selfsvc1", "password": NEW_PASSWORD},
    )
    assert res.status_code == 200, res.text
    # 旧口令失效
    res = await client.post(
        "/api/v1/admin/login",
        json={"username": "selfsvc1", "password": old_password},
    )
    assert res.status_code == 401, res.text


async def test_change_keeps_current_session_revokes_others(
    client, account_factory
) -> None:
    """改密后：当前会话仍有效，其它设备的会话被吊销。"""
    acct = await account_factory("selfsvc2", ["viewer"])
    other_token = await _seed_extra_session(acct["id"])

    # 改前其它设备可用
    res = await client.get(
        "/api/v1/admin/me", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert res.status_code == 200, res.text

    res = await client.post(
        "/api/v1/admin/me/password",
        json={"old_password": "member-pass-123", "new_password": NEW_PASSWORD},
        headers=acct["headers"],
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["revoked_sessions"] >= 1

    # 当前会话改后仍有效（不必重新登录）
    res = await client.get("/api/v1/admin/me", headers=acct["headers"])
    assert res.status_code == 200, res.text
    # 其它设备被踢
    res = await client.get(
        "/api/v1/admin/me", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert res.status_code == 401, res.text


async def test_wrong_old_password_is_400(client, account_factory) -> None:
    acct = await account_factory("selfsvc3", ["viewer"])
    res = await client.post(
        "/api/v1/admin/me/password",
        json={"old_password": "not-my-real-password", "new_password": NEW_PASSWORD},
        headers=acct["headers"],
    )
    assert res.status_code == 400, res.text


async def test_short_new_password_rejected_by_validation(client, account_factory) -> None:
    acct = await account_factory("selfsvc4", ["viewer"])
    res = await client.post(
        "/api/v1/admin/me/password",
        json={"old_password": "member-pass-123", "new_password": "short"},
        headers=acct["headers"],
    )
    assert res.status_code == 422, res.text


async def test_change_password_requires_login(client) -> None:
    res = await client.post(
        "/api/v1/admin/me/password",
        json={"old_password": "x", "new_password": NEW_PASSWORD},
    )
    assert res.status_code == 401, res.text
