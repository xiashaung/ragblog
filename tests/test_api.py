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
