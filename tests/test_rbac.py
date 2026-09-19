"""RBAC：权限矩阵、角色/账号管理、提权与锁死保护。

约定：
- 后台管理接口 = 登录会话（``Authorization: Bearer``）+ 权限码；
- 引导密钥（``APP_ADMIN_API_KEY``）只能用于 ``POST /admin/onboard``。
"""

from __future__ import annotations

from http import HTTPStatus

from app.rbac import ALL_PERMISSIONS, SUPERADMIN_ROLE, Permission


def _data(resp):
    """拆统一 envelope 拿 payload（失败响应直接读 ``["message"]``，不走这里）。"""
    assert 200 <= resp.status_code < 300, resp.text
    body = resp.json()
    if isinstance(body, dict) and "code" in body and "data" in body:
        assert body["data"] is not None, body
        return body["data"]
    return body


async def test_onboard_requires_bootstrap_key(client, console):
    # 引导密钥不对 → 403（第二个超管不能靠这个端点抢注）
    resp = await client.post(
        "/admin/onboard",
        json={"username": "hacker", "password": "hunter2hunter2", "name": "x"},
        headers={"X-API-Key": "wrong-bootstrap-key"},
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert "引导密钥" in resp.json()["message"]

    # 已经引导过一次：再拿正确密钥来也会被服务层拒绝
    resp = await client.post(
        "/admin/onboard",
        json={"username": "hacker", "password": "hunter2hunter2", "name": "x"},
        headers={"X-API-Key": "admin-test-key"},
    )
    assert resp.status_code == HTTPStatus.CONFLICT


async def test_admin_api_key_no_longer_grants_anything(client, console):
    """核心变化：那串 admin key 不再能管理任何东西。"""
    # 先清掉登录写入的 cookie，否则 cookie 旁路会让请求合法
    client.cookies.clear()
    resp = await client.get("/admin/accounts", headers={"X-API-Key": "admin-test-key"})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    resp = await client.post(
        "/admin/accounts",
        json={"username": "x", "password": "pass-word-123", "roles": []},
        headers={"X-API-Key": "admin-test-key"},
    )
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


async def test_login_returns_effective_permissions(client, console):
    assert console["roles"] == [SUPERADMIN_ROLE]
    assert console["permissions"] == ["*"]  # 超管走通配，不必逐条列举

    resp = await client.get("/admin/me", headers=console["headers"])
    assert resp.status_code == HTTPStatus.OK
    body = _data(resp)
    assert body["username"] == "root"
    assert body["is_superadmin"] is True
    assert body["permissions"] == ["*"]


async def test_builtin_roles_are_seeded_with_catalog_permissions(client, console):
    resp = await client.get("/admin/roles", headers=console["headers"])
    assert resp.status_code == HTTPStatus.OK
    roles = {item["code"]: item for item in _data(resp)}
    assert set(roles) >= {SUPERADMIN_ROLE, "admin", "operator", "viewer"}
    assert all(item["is_builtin"] for item in roles.values())
    # 内置 admin 有意不含 role:manage：能管账号但不能改角色
    assert Permission.ROLE_MANAGE not in roles["admin"]["permissions"]
    assert set(roles["viewer"]["permissions"]) == {
        Permission.ACCOUNT_READ,
        Permission.ROLE_READ,
        Permission.SESSION_READ,
    }

    resp = await client.get("/admin/permissions", headers=console["headers"])
    assert resp.status_code == HTTPStatus.OK
    assert {item["code"] for item in _data(resp)} == set(ALL_PERMISSIONS)


async def test_viewer_can_read_but_writes_are_denied(client, console, account_factory):
    viewer = await account_factory("viewer1", ["viewer"])
    headers = viewer["headers"]

    assert (await client.get("/admin/accounts", headers=headers)).status_code == HTTPStatus.OK
    assert (await client.get("/admin/roles", headers=headers)).status_code == HTTPStatus.OK
    assert (await client.get("/admin/sessions", headers=headers)).status_code == HTTPStatus.OK

    denied = await client.post(
        "/admin/accounts",
        json={"username": "x", "password": "pass-word-123", "roles": []},
        headers=headers,
    )
    assert denied.status_code == HTTPStatus.FORBIDDEN
    # 403 文案带中文名 + code，方便定位缺哪一条
    message = denied.json()["message"]
    assert "管理账号" in message and Permission.ACCOUNT_MANAGE in message

    assert (
        await client.delete("/admin/sessions/whatever", headers=headers)
    ).status_code == HTTPStatus.FORBIDDEN


async def test_role_manage_cannot_be_granted_by_non_superadmin(
    client, console, account_factory
):
    """admin 角色可以建账号，但不能造出一个带 role:manage 的角色给自己续命。"""
    admin = await account_factory("boss", ["admin"])
    assert Permission.ROLE_MANAGE not in admin["permissions"]
    assert Permission.ACCOUNT_MANAGE in admin["permissions"]

    # 能建账号
    resp = await client.post(
        "/admin/accounts",
        json={"username": "peer", "password": "pass-word-123", "roles": ["viewer"]},
        headers=admin["headers"],
    )
    assert resp.status_code == HTTPStatus.CREATED

    # 不能建角色（连 role:read 都有，但 role:manage 没有）
    resp = await client.post(
        "/admin/roles",
        json={"code": "sneaky", "name": "越权", "permissions": [Permission.ROLE_MANAGE]},
        headers=admin["headers"],
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN


async def test_superadmin_cannot_share_escalated_role(client, console, account_factory):
    """超管也不能把带 role:manage 的自定义角色分给他人 —— 除超管本人。"""
    resp = await client.post(
        "/admin/roles",
        json={"code": "godlike", "name": "全权", "permissions": ["*"]},
        headers=console["headers"],
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "超级管理员" in resp.json()["message"]

    # 只读角色可以正常创建并分配
    resp = await client.post(
        "/admin/roles",
        json={
            "code": "auditor",
            "name": "审计",
            "permissions": [Permission.SESSION_READ, Permission.ACCOUNT_READ],
        },
        headers=console["headers"],
    )
    assert resp.status_code == HTTPStatus.CREATED
    role = _data(resp)
    assert role["is_builtin"] is False

    member = await account_factory("auditor1", ["auditor"])
    assert set(member["permissions"]) == {Permission.SESSION_READ, Permission.ACCOUNT_READ}

    # 授权生效：它能看会话/账号，仍然不能建账号（缺 account:manage）
    assert (
        await client.get("/admin/sessions", headers=member["headers"])
    ).status_code == HTTPStatus.OK
    assert (
        await client.post(
            "/admin/accounts",
            json={"username": "x", "password": "pass-word-123", "roles": []},
            headers=member["headers"],
        )
    ).status_code == HTTPStatus.FORBIDDEN

    # 有成员在用 → 不允许删角色
    resp = await client.delete(f"/admin/roles/{role['id']}", headers=console["headers"])
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "使用" in resp.json()["message"]


async def test_permission_codes_are_validated_against_catalog(client, console):
    resp = await client.post(
        "/admin/roles",
        json={"code": "odd", "name": "脏数据", "permissions": ["kb:delete", "role:read"]},
        headers=console["headers"],
    )
    assert resp.status_code == HTTPStatus.CREATED
    # 未定义的 kb:delete 被丢掉，只留下代码目录里存在的 role:read
    assert _data(resp)["permissions"] == [Permission.ROLE_READ]


async def test_superadmin_role_is_locked(client, console):
    roles = {
        item["code"]: item
        for item in _data(await client.get("/admin/roles", headers=console["headers"]))
    }
    superadmin = roles[SUPERADMIN_ROLE]

    resp = await client.patch(
        f"/admin/roles/{superadmin['id']}",
        json={"permissions": [Permission.ACCOUNT_READ]},
        headers=console["headers"],
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "全部权限" in resp.json()["message"]

    resp = await client.delete(f"/admin/roles/{superadmin['id']}", headers=console["headers"])
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "内置角色" in resp.json()["message"]

    # 名称/描述可以改（不影响权限），方便本地化
    resp = await client.patch(
        f"/admin/roles/{superadmin['id']}", json={"name": "首席管理员"}, headers=console["headers"]
    )
    assert resp.status_code == HTTPStatus.OK
    assert _data(resp)["name"] == "首席管理员"
    assert _data(resp)["permissions"] == ["*"]


async def test_last_superadmin_cannot_lock_itself_out(client, console, account_factory):
    me = _data(await client.get("/admin/me", headers=console["headers"]))

    resp = await client.patch(
        f"/admin/accounts/{me['id']}", json={"is_active": False}, headers=console["headers"]
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "自己" in resp.json()["message"]

    resp = await client.delete(f"/admin/accounts/{me['id']}", headers=console["headers"])
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "自己" in resp.json()["message"]

    # 只剩一个超管时，不能把自己的超管角色收回去
    resp = await client.put(
        f"/admin/accounts/{me['id']}/roles",
        json={"roles": ["admin"]},
        headers=console["headers"],
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "最后一个超级管理员" in resp.json()["message"]

    # 有第二个超管接手后，就可以退下来了
    await account_factory("root2", [SUPERADMIN_ROLE])
    resp = await client.put(
        f"/admin/accounts/{me['id']}/roles",
        json={"roles": ["admin"]},
        headers=console["headers"],
    )
    assert resp.status_code == HTTPStatus.OK
    assert _data(resp)["roles"] == ["admin"]


async def test_disabling_account_revokes_its_sessions(
    client, console, account_factory
):
    member = await account_factory("ops", ["operator"])
    assert (await client.get("/admin/sessions", headers=member["headers"])).status_code == (
        HTTPStatus.OK
    )

    resp = await client.patch(
        f"/admin/accounts/{member['id']}", json={"is_active": False}, headers=console["headers"]
    )
    assert resp.status_code == HTTPStatus.OK
    assert _data(resp)["is_active"] is False

    blocked = await client.get("/admin/sessions", headers=member["headers"])
    assert blocked.status_code == HTTPStatus.UNAUTHORIZED

    # 停用后也无法登录
    login = await client.post(
        "/admin/login", json={"username": "ops", "password": "member-pass-123"}
    )
    assert login.status_code == HTTPStatus.UNAUTHORIZED

    # 重新启用后要显式登录才能再用
    await client.patch(
        f"/admin/accounts/{member['id']}", json={"is_active": True}, headers=console["headers"]
    )
    login = await client.post(
        "/admin/login", json={"username": "ops", "password": "member-pass-123"}
    )
    assert login.status_code == HTTPStatus.OK


async def test_revoke_single_session(client, console, account_factory):
    member = await account_factory("temp", ["viewer"])
    sessions = _data(await client.get("/admin/sessions", headers=console["headers"]))
    target = next(item for item in sessions if item["admin_id"] == member["id"])

    resp = await client.delete(
        f"/admin/sessions/{target['id']}", headers=console["headers"]
    )
    assert resp.status_code == HTTPStatus.OK
    assert (
        await client.get("/admin/accounts", headers=member["headers"])
    ).status_code == HTTPStatus.UNAUTHORIZED


async def test_role_membership_is_overwritten_not_merged(client, console, account_factory):
    member = await account_factory("switch", ["operator", "viewer"])
    assert Permission.SESSION_REVOKE in member["permissions"]  # 来自 operator
    assert Permission.ACCOUNT_READ in member["permissions"]  # 来自 viewer

    resp = await client.put(
        f"/admin/accounts/{member['id']}/roles",
        json={"roles": ["viewer"]},
        headers=console["headers"],
    )
    assert resp.status_code == HTTPStatus.OK
    assert _data(resp)["roles"] == ["viewer"]

    # 收回 operator 后失去 session:revoke
    assert (
        await client.delete("/admin/sessions/whatever", headers=member["headers"])
    ).status_code == HTTPStatus.FORBIDDEN

    # 未知角色名会被明确拒绝，避免拼错导致少授权
    resp = await client.put(
        f"/admin/accounts/{member['id']}/roles",
        json={"roles": ["superadminn"]},
        headers=console["headers"],
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "superadminn" in resp.json()["message"]


async def test_only_superadmin_can_assign_superadmin(client, console, account_factory):
    admin = await account_factory("boss2", ["admin"])
    resp = await client.post(
        "/admin/accounts",
        json={"username": "sneak", "password": "pass-word-123", "roles": [SUPERADMIN_ROLE]},
        headers=admin["headers"],
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "只有超级管理员" in resp.json()["message"]

    # 超管可以，但收回最后一个超管的角色会被拦住（见 test_superadmin_role_is_locked 同源保护）
    target = await account_factory("candidate", ["viewer"])
    resp = await client.put(
        f"/admin/accounts/{target['id']}/roles",
        json={"roles": [SUPERADMIN_ROLE]},
        headers=console["headers"],
    )
    assert resp.status_code == HTTPStatus.OK
    assert _data(resp)["roles"] == [SUPERADMIN_ROLE]

    # 现在有两个超管，可以收回其中一个
    resp = await client.put(
        f"/admin/accounts/{target['id']}/roles",
        json={"roles": ["admin"]},
        headers=console["headers"],
    )
    assert resp.status_code == HTTPStatus.OK
    assert _data(resp)["roles"] == ["admin"]


async def test_deleting_account_with_other_superadmin_online(client, console, account_factory):
    target = await account_factory("gone", ["viewer"])
    resp = await client.delete(f"/admin/accounts/{target['id']}", headers=console["headers"])
    assert resp.status_code == HTTPStatus.OK
    assert (
        await client.get("/admin/accounts", headers=target["headers"])
    ).status_code == HTTPStatus.UNAUTHORIZED
    usernames = {
        item["username"]
        for item in _data(await client.get("/admin/accounts", headers=console["headers"]))
    }
    assert "gone" not in usernames
