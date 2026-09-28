"""Test fixtures: mock LLM backend + a fresh SQLite DB per test + async client."""

import asyncio  # noqa: F401  (保留：历史 fixture 用过，部分测试模块间接依赖导入时机)
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import db
from app.config import Settings
from app.routers import admin as admin_router
from app.services import (
    admin_service,
    llm,
    provider_store,
)
from app.services import (
    system_config as system_config_service,
)


def _mock_settings() -> Settings:
    """A Settings object that routes every provider to the mock backend.

    ``_env_file=None`` bypasses the on-disk ``.env`` (which would otherwise
    override the defaults). ``validation_error=None`` lets us hand-construct
    a pydantic model without running its ``model_config`` (so we can then
    keep the nested provider blocks at their in-code defaults, which are all
    ``mock``).
    """
    return Settings(
        _env_file=None,
        debug=True,  # 打开示例 users/items 路由（生产默认不挂，见 main.create_app）
        llm_default_provider="mock",
        admin_api_key="admin-test-key",
        default_request_quota=3,
    )


@pytest.fixture()
def _patch_mock_backend() -> None:
    """Patch ``llm.make_backend`` to always return a ``MockBackend`` tagged
    with the requested provider, so tests don't need real network access.

    Tests can still select a specific provider in the request body; the
    backend is mock logic but its ``provider`` attribute reflects the
    requested name, so the ``provider`` field round-trips end-to-end.
    """
    from app.schemas.ai import Provider

    original = llm.make_backend

    def factory(provider: Provider):
        return llm.MockBackend(provider)

    llm.make_backend = factory
    llm.reset_registry()
    provider_store.reset()
    yield
    llm.make_backend = original
    llm.reset_registry()
    provider_store.reset()


@pytest.fixture()
async def client(
    monkeypatch, tmp_path: Path, _patch_mock_backend
) -> AsyncIterator[AsyncClient]:
    """Yields an AsyncClient bound to a freshly-built app with:

    - A file-backed SQLite database in ``tmp_path`` (fresh per test).
    - Mock LLM backend (all providers; only ``mock`` is truly offline in
      production).
    - Admin key "admin-test-key" and default quota 3.
    - Test mode: synchronous RAG queue execution for backward compatibility.
    """
    test_settings = _mock_settings()

    monkeypatch.setattr(llm, "get_settings", lambda: test_settings)
    monkeypatch.setattr(admin_service, "get_settings", lambda: test_settings)
    monkeypatch.setattr(admin_router, "get_settings", lambda: test_settings)
    # 配置相关的两个模块也读 settings：不一起打补丁的话，启动时 init_defaults()
    # 会把本机 .env 里的默认 provider（如 mlx）写进测试库，改变实际选用的模型。
    monkeypatch.setattr(system_config_service, "get_settings", lambda: test_settings)
    monkeypatch.setattr(provider_store, "get_settings", lambda: test_settings)
    llm.reset_registry()
    
    # 测试模式：路由层的 RAG 写入走同步执行（见 ai._sync_mode），不依赖后台消费；
    # 但队列 Worker 照常启动/停止——/health/ready 真探 Worker 存活，测试环境
    # 与生产的生命周期要保持一致。
    monkeypatch.setenv("APP_TEST_MODE", "true")

    # A fresh file-backed engine so the whole process (including the
    # app's own init_models call) uses a single connection pool and the
    # tables are visible to every request.
    db_file = tmp_path / "test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    db.engine = engine
    db.session_factory = factory

    from app.main import create_app

    app = create_app()

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    await engine.dispose()
    llm.reset_registry()


@pytest.fixture()
async def admin_headers(client, console) -> dict[str, str]:
    """AI / 知识库类接口的通用凭据：引导 Key + 超管会话，两个都带上。

    引导 Key（``X-API-Key``）只对 ``/api/v1/admin/onboard`` 有意义；知识库与模型配置接口
    已按 RBAC 真拦截（``knowledge:*`` / ``system:*``），光有引导 Key 会 401，
    所以这里复用 ``console`` 的超管 token（持有 ``*`` 通配，全量放行）。
    需要验证「无权限被拒」的用例请改用 ``account_factory`` 建普通账号。
    """
    return {**BOOTSTRAP_KEY_HEADERS, "Authorization": f"Bearer {console['token']}"}


BOOTSTRAP_KEY_HEADERS = {"X-API-Key": "admin-test-key"}
ROOT_PASSWORD = "root-pass-123"


def payload(resp) -> dict:
    """拆统一 envelope（中间件把所有 2xx 包成 {code,data,message}）。"""
    assert 200 <= resp.status_code < 300, resp.text
    body = resp.json()
    if isinstance(body, dict) and "code" in body and "data" in body:
        return body["data"]
    return body


@pytest.fixture()
async def console(client) -> dict:
    """Seed the first super-admin and return its console credentials.

    ``/api/v1/admin/onboard`` 需要引导密钥（与 ``APP_ADMIN_API_KEY`` 一致），
    之后所有管理接口靠 ``Authorization: Bearer <token>`` + 角色权限。
    """
    res = await client.post(
        "/api/v1/admin/onboard",
        json={"username": "root", "password": ROOT_PASSWORD, "name": "Root"},
        headers=BOOTSTRAP_KEY_HEADERS,
    )
    assert res.status_code == 200, res.text
    res = await client.post(
        "/api/v1/admin/login", json={"username": "root", "password": ROOT_PASSWORD}
    )
    assert res.status_code == 200, res.text
    body = payload(res)
    return {
        "token": body["token"],
        "headers": {"Authorization": f"Bearer {body['token']}"},
        "permissions": body["permissions"],
        "roles": body["roles"],
    }


@pytest.fixture()
def account_factory(client, console):
    """Create console accounts with given roles and return their bearer headers.

    Usage::

        acct = await account_factory("viewer1", ["viewer"])
        await client.get("/api/v1/admin/accounts", headers=acct["headers"])
    """

    async def make(
        username: str,
        roles: list[str],
        password: str = "member-pass-123",
    ) -> dict:
        res = await client.post(
            "/api/v1/admin/accounts",
            json={"username": username, "password": password, "name": username, "roles": roles},
            headers=console["headers"],
        )
        assert res.status_code == 201, res.text
        account_id = payload(res)["id"]
        res = await client.post(
            "/api/v1/admin/login", json={"username": username, "password": password}
        )
        assert res.status_code == 200, res.text
        body = payload(res)
        token = body["token"]
        return {
            "id": account_id,
            "username": username,
            "token": token,
            "headers": {"Authorization": f"Bearer {token}"},
            "permissions": body["permissions"],
        }

    return make
