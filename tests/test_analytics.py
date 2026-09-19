"""Tests for the standalone Text-to-SQL analytics feature.

Covers the security-critical pure guard, RBAC on the endpoints, SQLite schema
introspection with secret-column masking, and an end-to-end question → SQL →
read-only execution → conclusion flow (with a stubbed LLM so it stays offline).
"""

import json

from app.schemas.ai import Provider, Usage
from app.services import analytics

# --------------------------------------------------------------------------- #
# guard_read_only_sql —— 只读校验（安全核心，纯函数）
# --------------------------------------------------------------------------- #


def test_guard_accepts_plain_select():
    ok, sql, err = analytics.guard_read_only_sql("SELECT id, name FROM users;")
    assert ok, err
    assert sql == "SELECT id, name FROM users"  # 结尾分号被去掉


def test_guard_accepts_cte():
    ok, _sql, err = analytics.guard_read_only_sql(
        "WITH t AS (SELECT id FROM items) SELECT count(*) FROM t"
    )
    assert ok, err


def test_guard_strips_comments():
    ok, sql, err = analytics.guard_read_only_sql(
        "SELECT 1 AS one -- 注释里有个 ; 也不该算多语句\n"
    )
    assert ok, err
    assert sql.startswith("SELECT")


def test_guard_rejects_non_select():
    ok, _sql, err = analytics.guard_read_only_sql("UPDATE users SET name='x'")
    assert not ok
    assert "SELECT" in err or "只读" in err


def test_guard_rejects_multi_statement():
    ok, _sql, err = analytics.guard_read_only_sql("SELECT 1; DROP TABLE users")
    assert not ok
    assert "单条语句" in err


def test_guard_rejects_write_keyword_inside_cte():
    # Postgres 数据修改型 CTE：WITH x AS (DELETE ...) —— 必须拦下
    ok, _sql, err = analytics.guard_read_only_sql(
        "WITH x AS (DELETE FROM items RETURNING *) SELECT * FROM x"
    )
    assert not ok
    assert "DELETE" in err


def test_guard_rejects_side_effect_function():
    ok, _sql, err = analytics.guard_read_only_sql("SELECT pg_sleep(10)")
    assert not ok
    assert "PG_SLEEP" in err


def test_guard_rejects_denied_table():
    ok, _sql, err = analytics.guard_read_only_sql(
        "SELECT * FROM alembic_version", denied_tables=["alembic_version"]
    )
    assert not ok
    assert "不可访问" in err


def test_guard_allows_columns_hiding_forbidden_substrings():
    # created_at / is_active 里的 create 不是整词，不该被误伤
    ok, _sql, err = analytics.guard_read_only_sql(
        "SELECT created_at, is_active FROM users"
    )
    assert ok, err


def test_guard_rejects_blank():
    ok, _sql, err = analytics.guard_read_only_sql("   ;  ")
    assert not ok


def test_schema_for_prompt_includes_comments():
    # 表/列注释必须进入提示词，这是模型选对表的语义依据
    tables = [
        analytics.AnalyticsTable(
            name="oa_project_task",
            comment="项目任务表",
            columns=[
                analytics.AnalyticsColumn(name="flow_status", type="varchar", comment="审批状态"),
                analytics.AnalyticsColumn(name="id", type="int"),
            ],
        )
    ]
    text_ = analytics._schema_for_prompt(tables)
    assert "oa_project_task（项目任务表）" in text_
    assert "flow_status varchar「审批状态」" in text_
    assert "id int" in text_


# --------------------------------------------------------------------------- #
# 端点：鉴权 + 表结构脱敏
# --------------------------------------------------------------------------- #


async def test_analytics_requires_login(client):
    resp = await client.get("/api/v1/analytics/schema")
    assert resp.status_code == 401


async def test_analytics_forbidden_without_permission(client, account_factory):
    # viewer 无 analytics:read → 403
    acct = await account_factory("viewer-noperm", ["viewer"])
    resp = await client.get("/api/v1/analytics/schema", headers=acct["headers"])
    assert resp.status_code == 403


def _body(resp):
    data = resp.json()
    if isinstance(data, dict) and "code" in data and "data" in data:
        return data["data"]
    return data


async def test_analytics_schema_masks_credentials(client, console):
    resp = await client.get("/api/v1/analytics/schema", headers=console["headers"])
    assert resp.status_code == 200, resp.text
    body = _body(resp)
    names = {t["name"] for t in body["tables"]}
    # 业务表可问数；迁移表被隐藏
    assert {"users", "items"} <= names
    assert "alembic_version" not in names
    # 凭证列被从 schema 里剔除
    cols = {c["name"] for t in body["tables"] for c in t["columns"]}
    assert "password_hash" not in cols
    assert "token_hash" not in cols


# --------------------------------------------------------------------------- #
# 端到端问数（打桩 LLM，保持离线）
# --------------------------------------------------------------------------- #


class _StubBackend:
    """First chat → SQL JSON; second chat → conclusion text."""

    provider = Provider.MOCK

    def __init__(self, sql: str) -> None:
        self._sql = sql
        self.calls = 0

    async def chat(self, messages, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            content = json.dumps({"sql": self._sql, "brief": "取一行常量"})
        else:
            content = "查询到 1 行，值为 1。"
        usage = Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10)
        return content, "stop", usage, "stub"


async def test_analytics_query_end_to_end(client, console, monkeypatch):
    stub = _StubBackend("SELECT 1 AS one")
    monkeypatch.setattr(analytics.llm, "get_llm", lambda provider=None: stub)

    resp = await client.post(
        "/api/v1/analytics/query",
        json={"question": "现在有几条数据？"},
        headers=console["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = _body(resp)
    assert body["error"] is None
    assert body["sql"] == "SELECT 1 AS one"
    assert body["columns"] == ["one"]
    assert body["row_count"] == 1
    assert body["rows"] == [[1]]
    assert body["conclusion"]
    assert body["provider"] == "mock"


async def test_analytics_query_dry_run_skips_execution(client, console, monkeypatch):
    stub = _StubBackend("SELECT 1 AS one")
    monkeypatch.setattr(analytics.llm, "get_llm", lambda provider=None: stub)

    resp = await client.post(
        "/api/v1/analytics/query",
        json={"question": "只要 SQL", "dry_run": True},
        headers=console["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = _body(resp)
    assert body["error"] is None
    assert body["sql"] == "SELECT 1 AS one"
    # dry_run 不执行、不总结
    assert body["rows"] == []
    assert body["conclusion"] == ""


async def test_analytics_query_blocked_sql_returns_error(client, console, monkeypatch):
    # 模型若越权生成写语句，必须被只读校验挡下、且不执行
    stub = _StubBackend("DELETE FROM items")
    monkeypatch.setattr(analytics.llm, "get_llm", lambda provider=None: stub)

    resp = await client.post(
        "/api/v1/analytics/query",
        json={"question": "删掉所有商品"},
        headers=console["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = _body(resp)
    assert body["error"]
    assert "只读校验" in body["error"]
    assert body["rows"] == []


# --------------------------------------------------------------------------- #
# 幻觉字段的自愈重试 + 友好报错
# --------------------------------------------------------------------------- #


class _QueueStub:
    """按队列依次吐出 SQL；队列空了之后作为「总结」返回纯文本。"""

    provider = Provider.MOCK

    def __init__(self, sqls: list[str]) -> None:
        self._sqls = list(sqls)
        self.calls = 0

    async def chat(self, messages, **_kwargs):
        self.calls += 1
        if self._sqls:
            sql = self._sqls.pop(0)
            content = json.dumps({"sql": sql, "brief": "stub"})
        else:
            content = "查询完成。"
        usage = Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10)
        return content, "stop", usage, "stub"


async def test_query_self_repairs_unknown_column(client, console, monkeypatch):
    # 第一版引用了不存在的列，执行失败后应把报错回喂模型、修正后成功
    stub = _QueueStub(
        ["SELECT totally_missing_col FROM users", "SELECT 1 AS ok"]
    )
    monkeypatch.setattr(analytics.llm, "get_llm", lambda provider=None: stub)

    resp = await client.post(
        "/api/v1/analytics/query",
        json={"question": "随便查一下"},
        headers=console["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = _body(resp)
    assert body["error"] is None
    assert body["sql"] == "SELECT 1 AS ok"
    assert body["columns"] == ["ok"]
    assert body["conclusion"]


async def test_query_gives_up_with_friendly_error(client, console, monkeypatch):
    # 模型一直产出坏 SQL，超过修正上限后应返回中文友好提示，不泄露原始报错
    stub = _QueueStub(["SELECT nope_col FROM users"] * 10)
    monkeypatch.setattr(analytics.llm, "get_llm", lambda provider=None: stub)

    resp = await client.post(
        "/api/v1/analytics/query",
        json={"question": "随便查一下"},
        headers=console["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = _body(resp)
    assert body["error"]
    assert "字段" in body["error"]
    assert "no such column" not in body["error"].lower()
    assert "sqlalche.me" not in body["error"]


# --------------------------------------------------------------------------- #
# 对话式问数的历史持久化（存 / 回放 / 隔离 / 删除）
# --------------------------------------------------------------------------- #


async def _ask(client, headers, question):
    resp = await client.post(
        "/api/v1/analytics/query", json={"question": question}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return _body(resp)


async def test_history_requires_login(client):
    resp = await client.get("/api/v1/analytics/history")
    assert resp.status_code == 401


async def test_query_persists_history_and_replay(client, console, monkeypatch):
    stub = _QueueStub(["SELECT 1 AS one"])
    monkeypatch.setattr(analytics.llm, "get_llm", lambda provider=None: stub)

    body = await _ask(client, console["headers"], "有几个数字？")
    assert body["error"] is None
    assert body["chat_id"].startswith("ac-")
    assert body["created_at"] > 0

    resp = await client.get("/api/v1/analytics/history", headers=console["headers"])
    assert resp.status_code == 200, resp.text
    msgs = _body(resp)["messages"]
    assert len(msgs) == 1
    assert msgs[0]["question"] == "有几个数字？"
    assert msgs[0]["source"] == "main"
    assert msgs[0]["result"]["rows"] == [[1]]  # 完整结果可回放

    # 删除单条（204 无体）
    resp = await client.delete(
        f"/api/v1/analytics/history/{msgs[0]['id']}", headers=console["headers"]
    )
    assert resp.status_code == 204
    resp = await client.get("/api/v1/analytics/history", headers=console["headers"])
    assert _body(resp)["messages"] == []


async def test_history_isolated_between_users(client, console, account_factory, monkeypatch):
    stub = _QueueStub(["SELECT 1 AS one"])
    monkeypatch.setattr(analytics.llm, "get_llm", lambda provider=None: stub)
    await _ask(client, console["headers"], "超管的问题")

    other = await account_factory("analyst-isolated", ["admin"])
    resp = await client.get("/api/v1/analytics/history", headers=other["headers"])
    assert resp.status_code == 200, resp.text
    assert _body(resp)["messages"] == []  # 看不到别人的历史
    # 也删不掉别人的（静默无效，不是 500）
    resp = await client.delete(
        "/api/v1/analytics/history/ac_whatever", headers=other["headers"]
    )
    assert resp.status_code == 204


async def test_clear_history_removes_all(client, console, monkeypatch):
    # 两次提问：生成/总结交替消耗队列，保证两次都成功
    stub = _QueueStub(["SELECT 1 AS a", "SELECT 2 AS b", "SELECT 3 AS c"])
    monkeypatch.setattr(analytics.llm, "get_llm", lambda provider=None: stub)
    await _ask(client, console["headers"], "问题一")
    await _ask(client, console["headers"], "问题二")

    resp = await client.get("/api/v1/analytics/history", headers=console["headers"])
    assert len(_body(resp)["messages"]) == 2

    resp = await client.delete("/api/v1/analytics/history", headers=console["headers"])
    assert resp.status_code == 204
    resp = await client.get("/api/v1/analytics/history", headers=console["headers"])
    assert _body(resp)["messages"] == []
