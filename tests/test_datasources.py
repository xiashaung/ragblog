"""外部数据源（智能问数多库）的测试。

覆盖：连接串归一 / 掩码（纯函数）、CRUD + RBAC、问数页可选源列表，以及一次
针对**外部 SQLite 文件源**的端到端只读问数（打桩 LLM，离线）。
"""

import json
import sqlite3

from app.db import normalize_async_url
from app.schemas.ai import Provider, Usage
from app.services import analytics, datasource


def _data(resp):
    body = resp.json()
    if isinstance(body, dict) and "code" in body and "data" in body:
        return body["data"]
    return body


# --------------------------------------------------------------------------- #
# 纯函数：连接串归一 + 掩码
# --------------------------------------------------------------------------- #


def test_normalize_sync_to_async_drivers():
    assert normalize_async_url("mysql://u:p@h:3306/db", "mysql").startswith(
        "mysql+aiomysql://"
    )
    assert normalize_async_url(
        "mysql+pymysql://u:p@h/db", "mysql"
    ) == "mysql+aiomysql://u:p@h/db"
    assert normalize_async_url("sqlite:///x.db", "sqlite").startswith(
        "sqlite+aiosqlite://"
    )
    # 已是异步驱动则原样返回
    assert normalize_async_url("mysql+aiomysql://u:p@h/db", "mysql") == (
        "mysql+aiomysql://u:p@h/db"
    )


def test_mask_url_never_leaks_password():
    masked = datasource.mask_url("mysql+aiomysql://reader:S3cr3t@10.0.0.9:3306/wh")
    assert "S3cr3t" not in masked
    assert "10.0.0.9" in masked
    assert "wh" in masked


def test_mask_url_garbage_is_safe():
    assert datasource.mask_url("not a url") == "***"


# --------------------------------------------------------------------------- #
# CRUD + RBAC
# --------------------------------------------------------------------------- #


async def test_datasource_requires_login(client):
    resp = await client.get("/admin/data-sources")
    assert resp.status_code == 401


async def test_viewer_cannot_read_datasources(client, account_factory):
    acct = await account_factory("ds-viewer", ["viewer"])
    resp = await client.get("/admin/data-sources", headers=acct["headers"])
    assert resp.status_code == 403


async def test_admin_can_read_but_not_manage(client, account_factory):
    # admin 角色有 datasource:read 无 datasource:manage
    acct = await account_factory("ds-admin", ["admin"])
    ok = await client.get("/admin/data-sources", headers=acct["headers"])
    assert ok.status_code == 200
    created = await client.post(
        "/admin/data-sources",
        json={"name": "x", "dialect": "sqlite", "url": "sqlite:///x.db"},
        headers=acct["headers"],
    )
    assert created.status_code == 403


async def test_datasource_crud_and_masking(client, console):
    payload = {
        "name": "warehouse",
        "dialect": "mysql",
        "url": "mysql://reader:S3cr3t@10.0.0.9:3306/wh",
        "description": "只读数仓",
    }
    res = await client.post(
        "/admin/data-sources", json=payload, headers=console["headers"]
    )
    assert res.status_code == 201, res.text
    body = _data(res)
    assert body["id"].startswith("ds_")
    assert body["dialect"] == "mysql"
    assert body["url_masked"] and "S3cr3t" not in body["url_masked"]

    # 重名 → 400
    dup = await client.post(
        "/admin/data-sources", json=payload, headers=console["headers"]
    )
    assert dup.status_code == 400

    # 非法方言 → 400
    bad = await client.post(
        "/admin/data-sources",
        json={"name": "oracle1", "dialect": "oracle", "url": "x"},
        headers=console["headers"],
    )
    assert bad.status_code == 400

    # 列表含掩码串、不含明文口令
    lst = await client.get("/admin/data-sources", headers=console["headers"])
    assert lst.status_code == 200
    items = _data(lst)
    assert any(it["name"] == "warehouse" for it in items)
    assert all("S3cr3t" not in it["url_masked"] for it in items)

    # 更新：改名 + url 留空应沿用旧连接串（掩码里仍能看出 host 未被清掉）
    upd = await client.patch(
        f"/admin/data-sources/{body['id']}",
        json={"name": "warehouse2", "url": ""},
        headers=console["headers"],
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["data"]["name"] == "warehouse2"
    assert "10.0.0.9" in upd.json()["data"]["url_masked"]

    # 删除
    dele = await client.delete(
        f"/admin/data-sources/{body['id']}", headers=console["headers"]
    )
    assert dele.status_code == 200
    after = await client.get("/admin/data-sources", headers=console["headers"])
    assert all(it["id"] != body["id"] for it in _data(after))


# --------------------------------------------------------------------------- #
# 问数页可选源 + 针对外部 SQLite 源的端到端只读问数
# --------------------------------------------------------------------------- #


async def test_sources_endpoint_lists_main_first(client, console):
    resp = await client.get("/api/v1/analytics/sources", headers=console["headers"])
    assert resp.status_code == 200, resp.text
    items = _data(resp)
    assert items[0]["key"] == "main"
    assert items[0]["builtin"] is True


async def test_query_external_sqlite_source_end_to_end(
    client, console, tmp_path, monkeypatch
):
    # 造一个独立的外部 SQLite 文件，带一张业务表
    db_file = tmp_path / "ext.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE metrics (id INTEGER, val TEXT)")
    conn.executemany(
        "INSERT INTO metrics VALUES (?, ?)", [(1, "a"), (2, "b"), (3, "c")]
    )
    conn.commit()
    conn.close()

    created = await client.post(
        "/admin/data-sources",
        json={
            "name": "ext-sqlite",
            "dialect": "sqlite",
            "url": f"sqlite:///{db_file}",
        },
        headers=console["headers"],
    )
    assert created.status_code == 201, created.text
    source_id = _data(created)["id"]

    # 源列表应出现该外部源
    src = await client.get("/api/v1/analytics/sources", headers=console["headers"])
    keys = {it["key"] for it in _data(src)}
    assert source_id in keys

    class _Stub:
        provider = Provider.MOCK

        def __init__(self, sql):
            self._sql = sql
            self.calls = 0

        async def chat(self, messages, **_kwargs):
            self.calls += 1
            content = (
                json.dumps({"sql": self._sql, "brief": "数一下"})
                if self.calls == 1
                else "共 3 行。"
            )
            usage = Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
            return content, "stop", usage, "stub"

    monkeypatch.setattr(
        analytics.llm, "get_llm", lambda provider=None: _Stub("SELECT count(*) AS c FROM metrics")
    )

    resp = await client.post(
        "/api/v1/analytics/query",
        json={"question": "有多少条指标？", "source": source_id},
        headers=console["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = _data(resp)
    assert body["error"] is None, body["error"]
    assert body["source"] == source_id
    assert body["dialect"] == "sqlite"
    assert body["columns"] == ["c"]
    assert body["rows"] == [[3]]
    assert body["conclusion"]


async def test_query_unknown_source_returns_error(client, console, monkeypatch):
    class _Never:
        provider = Provider.MOCK

        async def chat(self, messages, **_kwargs):  # pragma: no cover
            raise AssertionError("源解析失败前不应调用模型")

    monkeypatch.setattr(analytics.llm, "get_llm", lambda provider=None: _Never())
    resp = await client.post(
        "/api/v1/analytics/query",
        json={"question": "x", "source": "ds-does-not-exist"},
        headers=console["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = _data(resp)
    assert body["error"] and "数据源" in body["error"]
