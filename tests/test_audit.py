"""管理写操作审计（轻量日志版）：动作落一行结构化 JSON，且绝不泄露口令。

审计走 ``logging.getLogger("audit")``，这里用 ``caplog`` 捕获并反解 JSON，
验三件事：① 关键写操作（登录/改密/建删账号/配置）都有对应 ``action``；
② 失败路径也记 ``result="fail"``；③ 口令等敏感值绝不进日志——只记字段名。
"""

from __future__ import annotations

import json
import logging
from http import HTTPStatus

from tests.conftest import ROOT_PASSWORD

AUDIT_PREFIX = "audit "


def _audit_events(caplog) -> list[dict]:
    """从捕获记录里挑出 audit logger 写的行，反解成 JSON 事件列表。"""
    events = []
    for rec in caplog.records:
        if rec.name != "audit":
            continue
        msg = rec.getMessage()
        assert msg.startswith(AUDIT_PREFIX), msg
        events.append(json.loads(msg[len(AUDIT_PREFIX) :]))
    return events


def _find(events: list[dict], action: str) -> list[dict]:
    return [e for e in events if e["action"] == action]


async def test_login_success_and_failure_audited(client, console, caplog) -> None:
    caplog.set_level(logging.INFO, logger="audit")
    # 成功登录（console 已登过 root，这里再登一次）
    await client.post("/api/v1/admin/login", json={"username": "root", "password": "wrong-pass"})
    ok = await client.post(
        "/api/v1/admin/login", json={"username": "root", "password": ROOT_PASSWORD}
    )
    assert ok.status_code == HTTPStatus.OK, ok.text
    events = _audit_events(caplog)
    fails = [e for e in _find(events, "auth.login") if e["result"] == "fail"]
    goods = [e for e in _find(events, "auth.login") if e["result"] == "ok"]
    assert fails and fails[0]["target"] == "root"
    assert goods and goods[0]["actor"] == "root"


async def test_account_delete_is_audited(client, console, account_factory, caplog) -> None:
    victim = await account_factory("audit-victim", ["viewer"])
    caplog.set_level(logging.INFO, logger="audit")
    res = await client.delete(
        f"/api/v1/admin/accounts/{victim['id']}", headers=console["headers"]
    )
    assert res.status_code == HTTPStatus.OK, res.text
    dele = _find(_audit_events(caplog), "account.delete")
    assert dele and dele[0]["target"] == victim["id"]
    assert dele[0]["actor"] == "root"
    assert dele[0]["result"] == "ok"


async def test_password_never_logged(
    client, console, account_factory, caplog
) -> None:
    """改密 / 重置口令只留动作与字段名，明文口令不得出现在任何审计行里。"""
    caplog.set_level(logging.INFO, logger="audit")
    # ① 管理员给他人重置口令（重置会踢掉该账号会话，故与②用不同账号）
    acct = await account_factory("audit-secret", ["viewer"])
    secret = "sup3r-s3cret-reset!"
    res = await client.patch(
        f"/api/v1/admin/accounts/{acct['id']}",
        json={"password": secret},
        headers=console["headers"],
    )
    assert res.status_code == HTTPStatus.OK, res.text
    # ② 另一个账号自己改密（保留当前会话）
    self_svc = await account_factory("audit-self", ["viewer"])
    old = "member-pass-123"
    new_secret = "brand-new-pa55word!"
    res = await client.post(
        "/api/v1/admin/me/password",
        json={"old_password": old, "new_password": new_secret},
        headers=self_svc["headers"],
    )
    assert res.status_code == HTTPStatus.OK, res.text

    events = _audit_events(caplog)
    assert _find(events, "account.update")
    assert _find(events, "account.password_change")
    # 两条口令明文都不能出现在任何审计事件的序列化结果里
    blob = json.dumps(events, ensure_ascii=False)
    assert secret not in blob
    assert new_secret not in blob
    # account.update 只记改了哪些字段名
    upd = _find(events, "account.update")[0]
    assert upd.get("changed") == ["password"]


async def test_config_update_is_audited(client, console, caplog) -> None:
    caplog.set_level(logging.INFO, logger="audit")
    res = await client.patch(
        "/api/v1/system/config/chat-provider",
        json={"value": "mock"},
        headers=console["headers"],
    )
    assert res.status_code == HTTPStatus.OK, res.text
    events = _find(_audit_events(caplog), "config.update")
    assert events and events[0]["actor"] == "root"
    assert events[0]["result"] == "ok"
