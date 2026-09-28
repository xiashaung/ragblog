"""AI 对话页的后端能力：会话持久化（仅本人可见）+ 真流式 NDJSON 产出。

约定：

- 会话接口只要登录态（不额外要求 RBAC 权限码），所以用 viewer 角色也能跑通；
- 跨身份的会话 id 一律 404 —— 服务层不区分「不存在」与「不是你的」，避免被用来
  探测他人的会话 id；
- 流式响应一旦开始就无法再改状态码，上游报错只能作为最后一行 chunk 透出。
"""

from __future__ import annotations

import json
from http import HTTPStatus

from app.routers import ai as ai_router
from app.schemas.ai import ChatChunk


def _data(resp):
    """拆统一信封拿 payload；非 2xx 直接把响应文本抛出来。"""
    assert 200 <= resp.status_code < 300, resp.text
    body = resp.json()
    if isinstance(body, dict) and "code" in body and "data" in body:
        return body["data"]
    return body


async def _new_session(client, headers, title: str = "") -> dict:
    res = await client.post("/api/v1/ai/chat/sessions", json={"title": title}, headers=headers)
    assert res.status_code == 200, res.text
    return _data(res)


def _lines(body: str) -> list[dict]:
    return [json.loads(line) for line in body.splitlines() if line.strip()]


async def test_chat_session_endpoints_require_login(client):
    res = await client.get("/api/v1/ai/chat/sessions")
    assert res.status_code == HTTPStatus.UNAUTHORIZED
    # 对话本体与 provider 目录也需登录（与文档口径一致）
    res = await client.post(
        "/api/v1/ai/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.status_code == HTTPStatus.UNAUTHORIZED
    res = await client.get("/api/v1/ai/providers")
    assert res.status_code == HTTPStatus.UNAUTHORIZED


async def test_session_round_trip_with_auto_title(client, console):
    session = await _new_session(client, console["headers"])
    assert session["title"] == "新对话"
    assert session["message_count"] == 0

    res = await client.post(
        f"/api/v1/ai/chat/sessions/{session['id']}/messages",
        headers=console["headers"],
        json={
            "messages": [
                {"role": "user", "content": "帮我用一句话解释什么是只读事务"},
                {
                    "role": "assistant",
                    "content": "只读事务里的写入会被数据库直接拒绝。",
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            ]
        },
    )
    assert res.status_code == 200, res.text
    body = _data(res)
    # 首条提问自动当标题（用户不改名也有意义），并刷新会话活跃时间
    assert body["session"]["title"].startswith("帮我用一句话解释")
    assert body["session"]["message_count"] == 2
    assert body["messages"][1]["usage"]["total_tokens"] == 15

    res = await client.get(
        f"/api/v1/ai/chat/sessions/{session['id']}/messages", headers=console["headers"]
    )
    msgs = _data(res)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]  # 正序，直接顺序渲染
    assert [m["seq"] for m in msgs] == [1, 2]

    res = await client.get("/api/v1/ai/chat/sessions", headers=console["headers"])
    listed = _data(res)["sessions"]
    assert [s["id"] for s in listed] == [session["id"]]
    assert listed[0]["message_count"] == 2  # 条数是统计出来的，不靠会话上的冗余计数

    res = await client.patch(
        f"/api/v1/ai/chat/sessions/{session['id']}",
        json={"title": "只读事务"},
        headers=console["headers"],
    )
    assert _data(res)["title"] == "只读事务"

    res = await client.delete(
        f"/api/v1/ai/chat/sessions/{session['id']}", headers=console["headers"]
    )
    assert res.status_code == HTTPStatus.NO_CONTENT
    # 会话连消息一起删；再操作都是 404
    res = await client.get(
        f"/api/v1/ai/chat/sessions/{session['id']}/messages", headers=console["headers"]
    )
    assert res.status_code == HTTPStatus.NOT_FOUND
    res = await client.delete(
        f"/api/v1/ai/chat/sessions/{session['id']}", headers=console["headers"]
    )
    assert res.status_code == HTTPStatus.NOT_FOUND


async def test_sessions_are_invisible_to_other_admins(
    client, console, account_factory
):
    mine = await _new_session(client, console["headers"], title="我的草稿会话")
    other = await account_factory("peeker", ["viewer"])

    res = await client.get("/api/v1/ai/chat/sessions", headers=other["headers"])
    assert _data(res)["sessions"] == []  # 列表里看不到

    res = await client.get(
        f"/api/v1/ai/chat/sessions/{mine['id']}/messages", headers=other["headers"]
    )
    assert res.status_code == HTTPStatus.NOT_FOUND  # 读不到，也不透露「存在但无权限」
    res = await client.delete(
        f"/api/v1/ai/chat/sessions/{mine['id']}", headers=other["headers"]
    )
    assert res.status_code == HTTPStatus.NOT_FOUND  # 删不掉

    res = await client.get(
        f"/api/v1/ai/chat/sessions/{mine['id']}/messages", headers=console["headers"]
    )
    assert res.status_code == 200  # 本人的不受影响


async def test_oversized_images_are_dropped_but_text_survives(client, console):
    session = await _new_session(client, console["headers"])
    huge = "data:image/png;base64," + "A" * 400_000  # 6 张 ≈ 2.4MB，越过 2MB 阈值
    res = await client.post(
        f"/api/v1/ai/chat/sessions/{session['id']}/messages",
        headers=console["headers"],
        json={"messages": [{"role": "user", "content": "看这张图", "images": [huge] * 6}]},
    )
    assert res.status_code == 200, res.text
    msg = _data(res)["messages"][0]
    assert msg["images"] == []
    assert msg["images_dropped"] == 6  # 明说「图片未持久化」，而不是默默缺图
    assert msg["content"] == "看这张图"  # 正文不能被附件连累

    # 正常体积的附件照旧入库
    res = await client.post(
        f"/api/v1/ai/chat/sessions/{session['id']}/messages",
        headers=console["headers"],
        json={
            "messages": [
                {"role": "user", "content": "这张小图呢", "images": ["data:image/png;base64,AAA"]}
            ]
        },
    )
    msg = _data(res)["messages"][0]
    assert msg["images"] == ["data:image/png;base64,AAA"]
    assert msg["seq"] == 2


async def test_append_to_missing_session_is_404(client, console):
    res = await client.post(
        "/api/v1/ai/chat/sessions/cs-not-exist/messages",
        headers=console["headers"],
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.status_code == HTTPStatus.NOT_FOUND


async def test_chat_stream_emits_ndjson_chunks(client, admin_headers):
    res = await client.post(
        "/api/v1/ai/chat",
        json={
            "provider": "mock",
            "messages": [{"role": "user", "content": "说点什么"}],
            "stream": True,
        },
        headers=admin_headers,
    )
    assert res.status_code == 200, res.text
    chunks = _lines(res.text)
    assert len(chunks) > 2, "回复被一次性打包说明又退化成假流式"
    text = "".join(c.get("content", "") for c in chunks)
    assert text.endswith("说点什么")  # 分块拼回完整回答，无丢字无重复
    tail = chunks[-1]
    assert tail["done"] is True
    assert tail["usage"]["total_tokens"] > 0


async def test_chat_stream_reports_upstream_error_as_last_chunk(
    client, admin_headers, monkeypatch
):
    """上游中途断流：已发的内容保留，报错作为最后一行 chunk 到达。"""

    class Flaky:
        provider = "mock"

        def chat_stream(self, messages, **kwargs):
            async def gen():
                yield ChatChunk(content="前半段")
                raise RuntimeError("上游连接被重置")

            return gen()

    monkeypatch.setattr(ai_router, "get_llm", lambda provider=None: Flaky())

    res = await client.post(
        "/api/v1/ai/chat",
        json={
            "provider": "mock",
            "messages": [{"role": "user", "content": "x"}],
            "stream": True,
        },
        headers=admin_headers,
    )
    assert res.status_code == 200, res.text  # 流已开始，状态码改不了
    chunks = _lines(res.text)
    assert chunks[0]["content"] == "前半段"
    assert chunks[-1]["done"] is True
    assert "上游连接被重置" in chunks[-1]["error"]
