"""Smoke tests for the API (user / item CRUD against the shared DB).

Every successful 2xx response is wrapped in the uniform envelope
``{"code": 0, "data": <payload>, "message": "success"}``; failure
responses carry ``code == <http_status>`` and ``data is None``.
Helpers here unwrap that envelope so the assertions read naturally.
"""


def _body(resp) -> dict:
    """Return the inner payload of a (possibly envelope-wrapped) response."""
    data = resp.json()
    if isinstance(data, dict) and "code" in data and "data" in data:
        return data["data"]
    return data


def _ok(resp):
    """Return the inner payload of a successful (2xx) response."""
    assert resp.status_code == 200, resp.text
    return _body(resp)


async def test_health(client) -> None:
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    assert _ok(resp) == {"status": "ok"}


async def test_user_crud(client) -> None:
    resp = await client.post(
        "/api/v1/users", json={"name": "Alice", "email": "alice@example.com"}
    )
    assert resp.status_code == 201
    user_id = _body(resp)["id"]
    assert _body(resp)["name"] == "Alice"

    resp = await client.get(f"/api/v1/users/{user_id}")
    assert resp.status_code == 200
    assert _ok(resp)["email"] == "alice@example.com"

    resp = await client.patch(f"/api/v1/users/{user_id}", json={"name": "Alicia"})
    assert resp.status_code == 200
    assert _ok(resp)["name"] == "Alicia"

    resp = await client.delete(f"/api/v1/users/{user_id}")
    assert resp.status_code == 204
    assert (await client.get(f"/api/v1/users/{user_id}")).status_code == 404


async def test_validation_error(client) -> None:
    resp = await client.post("/api/v1/users", json={"name": "", "email": "not-an-email"})
    assert resp.status_code == 422
    # Failure envelope: code mirrors the HTTP status, no data.
    err = resp.json()
    assert err["code"] == 422
    assert err["data"] is None


async def test_item_crud(client) -> None:
    resp = await client.post(
        "/api/v1/items",
        json={"title": "Widget", "description": "A thing", "price": 9.99},
    )
    assert resp.status_code == 201
    item_id = _body(resp)["id"]

    assert _ok(await client.get(f"/api/v1/items/{item_id}"))["price"] == 9.99
    assert (await client.delete(f"/api/v1/items/{item_id}")).status_code == 204


async def test_envelope_middleware_streams_ndjson_and_wraps_json():
    """中间件必须「流式逐块透传、只攒 application/json」——否则换云端模型后前端要等整个回答。

    直接用一个假 downstream ASGI app 驱动中间件，数外层 ``send`` 收到几个 body 消息：
    流式响应若被「攒齐再发」，这里就只会看到一个合并成整块的 body。
    """
    import json as _json

    from app.main import _EnvelopeMiddleware

    async def drive(content_type: bytes, chunks: list[bytes]):
        outer: list[dict] = []

        async def downstream(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", content_type)],
                }
            )
            for i, body in enumerate(chunks):
                await send(
                    {
                        "type": "http.response.body",
                        "body": body,
                        "more_body": i < len(chunks) - 1,
                    }
                )

        mw = _EnvelopeMiddleware(downstream)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            outer.append(message)

        await mw({"type": "http"}, receive, send)
        return outer

    # 流式（NDJSON 对话）：3 块各自透传，绝不合并成一块
    sent = await drive(b"application/x-ndjson", [b'{"a":1}\n', b'{"b":2}\n', b'{"c":3}\n'])
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert len(bodies) == 3, "流式响应被中间件攒齐了——前端就看不到逐字输出"
    assert [b["body"] for b in bodies] == [b'{"a":1}\n', b'{"b":2}\n', b'{"c":3}\n']

    # application/json 对象：攒齐后包成信封，只发一块
    sent = await drive(b"application/json", [b'{"a":1}'])
    final = [m for m in sent if m["type"] == "http.response.body"][-1]["body"]
    assert _json.loads(final) == {"code": 0, "data": {"a": 1}, "message": "success"}

    # 裸数组不包信封（原样过），否则前端 filter 全坏
    sent = await drive(b"application/json", [b"[1,2]"])
    final = [m for m in sent if m["type"] == "http.response.body"][-1]["body"]
    assert final == b"[1,2]"
