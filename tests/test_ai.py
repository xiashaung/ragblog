"""Tests for the AI endpoints (chat, embeddings, RAG) using the mock backend."""

import json

import pytest
from fastapi import status


def _dim() -> int:
    """向量列维度（mock 后端与之对齐，才能写进 Postgres 的定长 vector 列）。"""
    from app.config import get_settings

    return get_settings().rag_vector_dim


def _ok(resp):
    """Return the inner payload of a successful (2xx) response."""
    assert 200 <= resp.status_code < 300, resp.text
    data = resp.json()
    if isinstance(data, dict) and "code" in data and "data" in data:
        return data["data"]
    return data


def _body(resp):
    """Return the inner payload of a (possibly envelope-wrapped) response."""
    data = resp.json()
    if isinstance(data, dict) and "code" in data and "data" in data:
        return data["data"]
    return data


async def test_chat_with_admin_key_default_provider(client, admin_headers):
    resp = await client.post(
        "/api/v1/ai/chat",
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = _ok(resp)
    assert body["provider"] == "mock"
    # The mock echoes the last message prefixed with provider:model, where
    # ``model`` is the per-provider default (mock-chat for the mock slot).
    assert body["content"].startswith("[mock:mock-chat]")
    assert body["usage"]["total_tokens"] > 0


async def test_chat_with_explicit_provider(client, admin_headers):
    # In tests the mock backend is tagged with the requested provider name,
    # so we can verify the round-trip for every provider in the enum.
    for name in ("mock", "openai", "deepseek", "qwen", "ollama", "mlx"):
        resp = await client.post(
            "/api/v1/ai/chat",
            json={
                "provider": name,
                "messages": [{"role": "user", "content": f"hello from {name}"}],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200, f"provider={name} -> {resp.status_code}"
        body = _ok(resp)
        assert body["provider"] == name
        # The mock echoes the last user message; the prefix shows the
        # provider + resolved model from the per-provider config slot.
        assert f"[{name}:" in body["content"]


async def test_chat_unknown_provider_returns_400(client, admin_headers):
    resp = await client.post(
        "/api/v1/ai/chat",
        json={
            "provider": "made-up",
            "messages": [{"role": "user", "content": "hello"}],
        },
        headers=admin_headers,
    )
    # Pydantic enum validation rejects the value
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


async def test_chat_structured_output(client, admin_headers):
    resp = await client.post(
        "/api/v1/ai/chat",
        json={
            "messages": [{"role": "user", "content": "summarize"}],
            "response_format": {"type": "json_object"},
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200
    parsed = json.loads(_ok(resp)["content"])
    assert "summary" in parsed


async def test_embeddings_default_provider(client, admin_headers):
    resp = await client.post(
        "/api/v1/ai/embeddings",
        json={"input": ["hello world", "fastapi is great"]},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = _ok(resp)
    assert body["provider"] == "mock"
    assert len(body["items"]) == 2
    assert len(body["items"][0]["embedding"]) == _dim()


def test_chunk_text_bounds_every_chunk():
    """无换行的长文本也要按 chunk_size 切开，块间保留重叠窗口。"""
    from app.services import rag

    text = "".join(f"S{i:05d}" for i in range(800))
    chunks = rag.chunk_text(text, chunk_size=500, overlap=80)
    assert len(chunks) > 1
    assert all(0 < len(c) <= 500 for c in chunks)
    # 上一块的尾部出现在下一块开头（重叠），且最后一块抵达文末
    assert chunks[0][-20:] in chunks[1]
    assert text[-20:] in chunks[-1]

    # 短文档保持原样
    assert rag.chunk_text("hello\nworld", chunk_size=500) == ["hello\nworld"]


async def test_rag_ingest_and_answer(client, admin_headers):
    resp = await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={
            "title": "Guide",
            "content": "The capital of France is Paris. Paris is famous for the Eiffel Tower. "
            * 20,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = _ok(resp)
    assert body["provider"] == "mock"
    kb_id = body["knowledge_id"]

    resp = await client.post(
        "/api/v1/ai/knowledge/search",
        json={"query": "Paris", "top_k": 3},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = _ok(resp)
    assert body["provider"] == "mock"
    assert body["items"]

    resp = await client.post(
        "/api/v1/ai/knowledge/answer",
        json={"query": "What is the capital of France?", "top_k": 3},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = _ok(resp)
    assert body["provider"] == "mock"
    assert body["answer"]
    assert body["citations"]

    resp = await client.delete(f"/api/v1/ai/knowledge/{kb_id}", headers=admin_headers)
    assert resp.status_code == 200


async def test_rag_provider_scopes_search(client, admin_headers):
    # Ingest the same content under two different provider slots.
    resp = await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={
            "provider": "mock",
            "title": "A-mock",
            "content": "Paris is the capital of France. " * 3,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200
    kb_mock = _ok(resp)["knowledge_id"]
    assert _ok(resp)["provider"] == "mock"

    resp = await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={
            "provider": "deepseek",
            "title": "B-deepseek",
            "content": "Tokyo is the capital of Japan. " * 3,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200
    kb_ds = _ok(resp)["knowledge_id"]
    assert _ok(resp)["provider"] == "deepseek"

    # Searching with provider=mock should only see the mock doc...
    resp = await client.post(
        "/api/v1/ai/knowledge/search",
        json={"provider": "mock", "query": "Paris", "top_k": 5},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    items = _ok(resp)["items"]
    seen = {c["knowledge_id"] for c in items}
    assert kb_mock in seen
    assert kb_ds not in seen  # deepseek doc is out of scope for a mock search

    # ...and with provider=deepseek only the deepseek doc.
    resp = await client.post(
        "/api/v1/ai/knowledge/search",
        json={"provider": "deepseek", "query": "Paris", "top_k": 5},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    items = _ok(resp)["items"]
    seen = {c["knowledge_id"] for c in items}
    assert kb_ds in seen
    assert kb_mock not in seen


async def test_stats_reports_vector_setup(client, admin_headers):
    """统计要能说清：向量存在哪、多少维、有没有拿不到向量的分片。"""
    body = _ok(
        await client.get("/api/v1/ai/knowledge/stats", headers=admin_headers)
    )
    assert body["vector_backend"] in {"pgvector", "json"}
    assert body["vector_dim"] == _dim()
    assert body["vectorless_chunks"] >= 0


async def test_dimension_mismatch_is_rejected_before_chunks_are_wiped(
    client, admin_headers, monkeypatch
):
    """新向量不可用时，不能先把旧分片删掉。"""
    from app.services import rag

    resp = await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={"title": "维度保护", "content": "巴黎是法国的首都。" * 10},
        headers=admin_headers,
    )
    kb_id = _ok(resp)["knowledge_id"]

    monkeypatch.setattr(rag, "_vector_dim", lambda: 64)
    resp = await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={"title": "维度保护", "content": "柏林是德国的首都。" * 10},
        headers=admin_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    message = resp.json()["message"]
    assert "64" in message and str(_dim()) in message

    detail = _ok(
        await client.get(f"/api/v1/ai/knowledge/{kb_id}", headers=admin_headers)
    )
    assert detail["chunk_total"] >= 1
    assert all(chunk["dim"] == _dim() for chunk in detail["chunks"])

    await client.delete(f"/api/v1/ai/knowledge/{kb_id}", headers=admin_headers)


def _needs_postgres() -> bool:
    from app.config import get_settings

    return not get_settings().database_url.startswith("postgresql")


@pytest.mark.skipif(_needs_postgres(), reason="pgvector 路径需要 Postgres")
async def test_pgvector_native_scoring_matches_python_cosine():
    """库内 ``<=>`` 的打分要和 Python 余弦一致，且 HNSW 索引确实建了。

    跑在用户真实库上，所以全部写在一段未提交的事务里，结束就 rollback。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.config import get_settings
    from app.db import HNSW_INDEX_NAME
    from app.models import KnowledgeChunk
    from app.schemas.ai import EmbeddingRequest
    from app.services import llm, rag

    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            session = AsyncSession(bind=conn, expire_on_commit=False)
            assert rag._uses_pgvector(session)

            doc_ids = []
            samples = (("pgvec-A", "巴黎是法国的首都。"), ("pgvec-B", "东京是日本的首都。"))
            for title, body in samples:
                resp = await rag.upsert_document(
                    session, title=title, content=body * 5, provider="mock"
                )
                doc_ids.append(resp.knowledge_id)

            backend = llm.get_llm("mock")
            query = (await backend.embeddings(
                EmbeddingRequest(provider=backend.provider, input=["巴黎"])
            )).items[0].embedding

            native = await rag._search_pgvector(session, doc_ids, query, 5)
            python = await rag._search_in_python(session, doc_ids, query, 5)
            assert len(native) == len(python) == 2
            # 排序一致，且分数只差在 pgvector 的 float4 存储精度上
            assert [hit[0] for hit in native] == [hit[0] for hit in python]
            for native_hit, python_hit in zip(native, python, strict=True):
                assert native_hit[3] == pytest.approx(python_hit[3], abs=1e-5)

            index_def = (
                await session.execute(
                    text(
                        "select indexdef from pg_indexes where indexname = :name"
                    ),
                    {"name": HNSW_INDEX_NAME},
                )
            ).scalar()
            assert index_def and "hnsw" in index_def

            # 列维度就是配置里的维度（定长 vector 列）
            stored = (
                await session.execute(
                    text(
                        "select format_type(atttypid, atttypmod) from pg_attribute"
                        " where attrelid = 'knowledge_chunks'::regclass"
                        " and attname = 'vector'"
                    )
                )
            ).scalar()
            assert stored == f"vector({_dim()})"

            # 验证过 NULL 向量不会进入打分（先抹掉一篇的向量再查）
            await session.execute(
                KnowledgeChunk.__table__.update()
                .where(KnowledgeChunk.knowledge_id == doc_ids[1])
                .values(vector=None)
            )
            assert {hit[0] for hit in await rag._search_pgvector(session, doc_ids, query, 5)} == {
                doc_ids[0]
            }

            await tx.rollback()
    finally:
        await engine.dispose()


async def test_knowledge_management(client, admin_headers):
    """列表分页/搜索、详情、改名、改内容、统计、重建索引、批量删除。"""
    resp = await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={
            "title": "Guide",
            "content": "Paris is the capital of France. " * 30,
            "chunk_size": 200,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200
    created = _ok(resp)
    kb_id = created["knowledge_id"]
    assert created["chars"] == len("Paris is the capital of France. " * 30)

    # --- list: keyword + pagination envelope ---
    resp = await client.get(
        "/api/v1/ai/knowledge",
        params={"keyword": "Guide", "offset": 0, "limit": 5},
        headers=admin_headers,
    )
    body = _ok(resp)
    assert body["default_provider"] == "mock"
    assert body["total"] >= 1 and len(body["items"]) <= 5
    row = next(i for i in body["items"] if i["knowledge_id"] == kb_id)
    assert row["has_content"] and row["chars"] > 0 and row["chunks"] > 1
    assert row["stale"] is False and row["preview"]

    # keyword that matches nothing
    resp = await client.get(
        "/api/v1/ai/knowledge", params={"keyword": "nope-xyz"}, headers=admin_headers
    )
    assert _ok(resp)["total"] == 0

    # --- detail: original text + chunk bodies ---
    resp = await client.get(f"/api/v1/ai/knowledge/{kb_id}", headers=admin_headers)
    detail = _ok(resp)
    assert detail["content"].startswith("Paris")
    assert detail["chunk_total"] == row["chunks"]
    assert [c["index"] for c in detail["chunks"]] == list(range(detail["chunk_total"]))

    # --- rename keeps citations pointing at the new title ---
    resp = await client.patch(
        f"/api/v1/ai/knowledge/{kb_id}",
        json={"title": "Guide v2"},
        headers=admin_headers,
    )
    assert _ok(resp)["title"] == "Guide v2"
    resp = await client.get(f"/api/v1/ai/knowledge/{kb_id}", headers=admin_headers)
    assert {c["source"] for c in _ok(resp)["chunks"]} == {"Guide v2"}

    # --- replace content re-embeds the document ---
    resp = await client.patch(
        f"/api/v1/ai/knowledge/{kb_id}",
        json={"content": "Berlin is the capital of Germany."},
        headers=admin_headers,
    )
    assert _ok(resp)["chars"] == len("Berlin is the capital of Germany.")
    resp = await client.get(f"/api/v1/ai/knowledge/{kb_id}", headers=admin_headers)
    detail = _ok(resp)
    assert detail["content"] == "Berlin is the capital of Germany."
    assert detail["chunk_total"] == 1

    # --- stats ---
    resp = await client.get("/api/v1/ai/knowledge/stats", headers=admin_headers)
    body = _ok(resp)
    assert body["docs"] >= 1 and body["chunks"] >= 1
    assert body["default_provider"] == "mock"
    assert {p["provider"] for p in body["by_provider"]} >= {"mock"}

    # --- reindex rebuilds vectors with the configured provider ---
    resp = await client.post(
        f"/api/v1/ai/knowledge/{kb_id}/reindex", headers=admin_headers
    )
    body = _ok(resp)
    assert body["provider"] == "mock" and body["chunks"] == 1

    # --- empty patch is rejected ---
    resp = await client.patch(
        f"/api/v1/ai/knowledge/{kb_id}", json={}, headers=admin_headers
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST

    # --- missing document: 404 on every detail route ---
    missing = "kb-does-not-exist"
    for method, url in (
        ("get", f"/api/v1/ai/knowledge/{missing}"),
        ("patch", f"/api/v1/ai/knowledge/{missing}"),
        ("post", f"/api/v1/ai/knowledge/{missing}/reindex"),
        ("delete", f"/api/v1/ai/knowledge/{missing}"),
    ):
        resp = await client.request(
            method.upper(), url, headers=admin_headers, json={"title": "x"}
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND, f"{method} {url}"
        assert "不存在" in resp.json()["message"]

    # --- batch delete reports which ids were not found ---
    resp = await client.post(
        "/api/v1/ai/knowledge/batch-delete",
        json={"ids": [kb_id, missing]},
        headers=admin_headers,
    )
    body = _ok(resp)
    assert body["deleted"] == 1 and body["failed"] == [missing]


async def test_knowledge_stale_flag_tracks_default(client, admin_headers):
    """文档的 provider 与后端默认不一致时标记 stale，可只筛这部分。"""
    resp = await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={"provider": "deepseek", "title": "Other", "content": "Tokyo text. " * 5},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    resp = await client.get(
        "/api/v1/ai/knowledge", params={"only_stale": True}, headers=admin_headers
    )
    items = _ok(resp)["items"]
    assert [i["title"] for i in items] == ["Other"]
    assert items[0]["stale"] is True

    resp = await client.get(
        "/api/v1/ai/knowledge", params={"provider": "mock"}, headers=admin_headers
    )
    assert _ok(resp)["items"] == []


def test_html_to_text_keeps_article_and_drops_chrome():
    """抽正文：保留标题与段落，丢弃导航/页脚/脚本/样式。"""
    from app.services.web_page import html_to_text

    html = """<html><head><title> 安装指南 </title>
    <style>body{color:red}</style><script>var secret = 1;</script></head>
    <body><nav>首页 文档 登录</nav>
    <h1>安装指南</h1>
    <p>先安装 <b>Python</b>，再执行 pip install -e .</p>
    <p>&nbsp;</p>
    <footer>版权所有</footer></body></html>"""

    title, text = html_to_text(html)
    assert title == "安装指南"
    assert "pip install" in text
    assert "先安装 Python，再执行 pip install -e ." in text
    for noise in ("首页", "版权所有", "color:red", "var secret", "secret"):
        assert noise not in text


async def test_fetch_page_requires_http():
    """只允许 http/https，避免 file:// 等本地地址被当作录入源。"""
    import pytest

    from app.services.web_page import fetch_page

    with pytest.raises(ValueError, match="http"):
        await fetch_page("file:///etc/passwd")


async def test_knowledge_ingest_pairs(client, admin_headers):
    """问答对录入：合并成一篇 FAQ，问句在前，可检索可编辑。"""
    resp = await client.post(
        "/api/v1/ai/knowledge/ingest-pairs",
        json={
            "title": "退款政策 FAQ",
            "pairs": [
                {
                    "question": "订单多久可以退款？",
                    "answer": "签收后 7 个工作日内可申请全额退款。",
                },
                {
                    "question": "退款到哪个账户？",
                    "answer": "原路退回支付账户，银行处理需 1-3 个工作日。",
                },
                {"question": "运费能退吗？", "answer": "质量问题导致的运费由商家承担。"},
            ],
        },
        headers=admin_headers,
    )
    body = _ok(resp)
    assert body["chunks"] >= 1
    kb = body["knowledge_id"]

    detail = _ok(await client.get(f"/api/v1/ai/knowledge/{kb}", headers=admin_headers))
    assert detail["doc"]["kind"] == "faq"
    assert detail["doc"]["metadata"]["pairs"] == 3
    assert detail["content"].startswith("问：")
    assert "答：原路退回支付账户" in detail["content"]

    listed = _ok(await client.get("/api/v1/ai/knowledge", headers=admin_headers))
    row = next(i for i in listed["items"] if i["knowledge_id"] == kb)
    assert row["kind"] == "faq" and row["has_content"] is True

    hits = _ok(
        await client.post(
            "/api/v1/ai/knowledge/search",
            json={"query": "退款期限", "top_k": 3},
            headers=admin_headers,
        )
    )["items"]
    assert kb in {h["knowledge_id"] for h in hits}

    # 问答对全为空（答案只剩空格）时拒绝入库
    resp = await client.post(
        "/api/v1/ai/knowledge/ingest-pairs",
        json={"title": "空 FAQ", "pairs": [{"question": "q", "answer": "   "}]},
        headers=admin_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "问答对" in resp.json()["message"]


async def test_knowledge_fetch_url(client, admin_headers, monkeypatch):
    """URL 录入：抓取正文入库，标题默认取页面 <title>。"""
    from app.services import web_page

    page = web_page.PageText(
        url="http://example.com/deploy",
        final_url="http://example.com/deploy",
        status=200,
        title="部署手册",
        text="\n".join(
            f"第 {i} 步：执行 uvicorn app.main:app 启动服务，必要时修改 .env 中的数据库地址。"
            for i in range(1, 12)
        ),
    )

    captured: dict = {}

    async def fake_fetch(url, *, max_chars=None, timeout=15.0):
        captured["url"] = url
        captured["max_chars"] = max_chars
        return page

    monkeypatch.setattr(web_page, "fetch_page", fake_fetch)

    resp = await client.post(
        "/api/v1/ai/knowledge/fetch-url",
        json={"url": "http://example.com/deploy", "chunk_size": 200},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = _ok(resp)
    assert captured["url"] == "http://example.com/deploy"
    assert body["title"] == "部署手册"
    assert body["chunks"] > 1
    assert body["final_url"] == "http://example.com/deploy"

    listed = _ok(await client.get("/api/v1/ai/knowledge", headers=admin_headers))
    row = next(i for i in listed["items"] if i["knowledge_id"] == body["knowledge_id"])
    assert row["kind"] == "url"
    assert row["source_url"] == "http://example.com/deploy"


async def test_knowledge_fetch_url_reports_bad_link(client, admin_headers, monkeypatch):
    """抓取失败要返回中文原因，而不是 500。"""
    from app.services import web_page

    async def fake_fetch(url, *, max_chars=None, timeout=15.0):
        raise ValueError("页面返回 HTTP 404，无法入库")

    monkeypatch.setattr(web_page, "fetch_page", fake_fetch)

    resp = await client.post(
        "/api/v1/ai/knowledge/fetch-url",
        json={"url": "http://example.com/missing"},
        headers=admin_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "404" in resp.json()["message"]


async def test_upload_batch_reports_per_file(client, admin_headers):
    """批量上传：好文件照常入库，坏文件单独报错。"""
    files = [
        (
            "files",
            (
                "手册a.md",
                ("# 标题A\n\n" + "这是用于批量入库验证的文档正文内容，足够长。" * 5).encode(),
                "text/markdown",
            ),
        ),
        (
            "files",
            (
                "文本b.txt",
                ("这是第二个批量入库的文本文件，内容也足够长。" * 4).encode(),
                "text/plain",
            ),
        ),
        ("files", ("坏文件.exe", b"MZ\x00\x01", "application/octet-stream")),
    ]
    resp = await client.post(
        "/api/v1/upload/batch",
        files=files,
        data={"chunk_size": "300"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = _ok(resp)
    assert (body["total"], body["succeeded"], body["failed"]) == (3, 2, 1)

    bad = next(r for r in body["results"] if r["status"] == "error")
    assert bad["filename"] == "坏文件.exe"
    assert "不支持" in bad["error"]

    listed = _ok(
        await client.get(
            "/api/v1/ai/knowledge", params={"limit": 50}, headers=admin_headers
        )
    )["items"]
    by_title = {i["title"]: i for i in listed}
    # 标题默认取文件名主干，同源重传会覆盖而不是重复
    assert {"手册a", "文本b"} <= set(by_title)
    assert by_title["手册a"]["kind"] == "file"
    assert by_title["手册a"]["source_file"] == "手册a.md"


async def test_upload_batch_rejects_too_many_files(client, admin_headers):
    files = [
        (
            "files",
            (f"f{i}.txt", ("正文内容需要超过十个字符才能入库。" * 2).encode(), "text/plain"),
        )
        for i in range(21)
    ]
    resp = await client.post("/api/v1/upload/batch", files=files, headers=admin_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "20" in resp.json()["message"]


async def test_registry_returns_same_instance_for_same_provider(client, admin_headers):
    """Two requests to the same provider share the cached backend."""
    from app.services import llm

    backend = llm.get_llm()
    assert llm.get_llm() is backend
    # Different provider (also mock in tests, but a different slot) is a
    # distinct registry entry.
    from app.schemas.ai import Provider

    other = llm.get_llm(Provider.MOCK)
    assert other is backend  # same provider enum → same instance


async def test_registry_resets(client, admin_headers):
    from app.services import llm

    a = llm.get_llm()
    llm.reset_registry()
    b = llm.get_llm()
    assert a is not b


async def test_chat_accepts_multimodal_content_parts(client, admin_headers):
    """带图片的 content parts 能解析；mock 后端把图片降级为 [image] 占位。"""
    resp = await client.post(
        "/api/v1/ai/chat",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "这张图里是什么？"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                }
            ]
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = _ok(resp)
    assert "这张图里是什么" in body["content"]
    assert "[image]" in body["content"]


async def test_rag_answer_accepts_images(client, admin_headers):
    """知识问答带图：answer 请求接受 images 并正常返回。"""
    await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={"title": "图文", "content": "巴黎是法国的首都。" * 10},
        headers=admin_headers,
    )
    resp = await client.post(
        "/api/v1/ai/knowledge/answer",
        json={"query": "首都？", "top_k": 3, "images": ["data:image/png;base64,AAAA"]},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert _ok(resp)["answer"]


async def test_upload_image_ingests_description_and_saves_file(
    client, admin_headers, monkeypatch, tmp_path
):
    """图片上传：视觉模型生成描述入库，原图落盘、meta 只存 URL（非 blob）。"""
    from app.config import get_settings
    from app.routers import upload as upload_mod

    monkeypatch.setattr(get_settings(), "media_dir", str(tmp_path))

    async def fake_describe(data_url, provider):
        assert data_url.startswith("data:image/png;base64,")
        return "这是一张埃菲尔铁塔的照片，背景是蓝天，前景矗立着铁塔的钢架结构。"

    monkeypatch.setattr(upload_mod, "_describe_image", fake_describe)

    resp = await client.post(
        "/api/v1/upload",
        files={"file": ("铁塔.png", b"\x89PNG\r\n\x1a\nfakepngbytes", "image/png")},
        data={"title": "铁塔图片"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = _ok(resp)
    assert body["status"] == "success", body
    kb_id = body["knowledge_id"]

    detail = _ok(
        await client.get(f"/api/v1/ai/knowledge/{kb_id}", headers=admin_headers)
    )
    assert detail["doc"]["kind"] == "image"
    assert "埃菲尔铁塔" in detail["content"]
    image_ref = detail["doc"]["metadata"]["images"][0]
    assert image_ref.startswith("/media/uploads/")
    assert (tmp_path / "uploads" / image_ref.rsplit("/", 1)[-1]).is_file()


async def test_parse_docx_full_extracts_text_and_images():
    """解析器能从 DOCX 同时抽出正文与内嵌图片。"""
    import base64
    import io

    from docx import Document

    from app.services import document_parser

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph("表格与图片测试正文，需要足够长才能通过入库阈值。" * 3)
    doc.add_picture(io.BytesIO(png))
    doc.save(buf)

    parsed = await document_parser.parse_document_full(io.BytesIO(buf.getvalue()), "t.docx")
    assert "表格与图片测试正文" in parsed.text
    assert len(parsed.images) == 1
    assert parsed.images[0].ext == "png"


async def test_upload_docx_describes_embedded_images(
    client, admin_headers, monkeypatch, tmp_path
):
    """文档内嵌图片：抽出→视觉模型描述并入正文，原图落盘、meta.images 存 URL。"""
    import base64
    import io

    from docx import Document

    from app.config import get_settings
    from app.routers import upload as upload_mod

    monkeypatch.setattr(get_settings(), "media_dir", str(tmp_path))

    async def fake_describe(data_url, provider):
        assert data_url.startswith("data:image/")
        return "这是一张销售趋势折线图，2024 年逐季度稳步上升。"

    monkeypatch.setattr(upload_mod, "_describe_image", fake_describe)

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph("本季度业务回顾如下。" * 6)
    doc.add_picture(io.BytesIO(png))
    doc.save(buf)

    resp = await client.post(
        "/api/v1/upload",
        files={
            "file": (
                "回顾.docx",
                buf.getvalue(),
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document",
            )
        },
        data={"title": "季度回顾图文"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = _ok(resp)
    assert body["status"] == "success", body
    kb = body["knowledge_id"]

    detail = _ok(await client.get(f"/api/v1/ai/knowledge/{kb}", headers=admin_headers))
    assert "销售趋势折线图" in detail["content"]
    assert detail["doc"]["metadata"]["embedded_images"] >= 1
    assert detail["doc"]["metadata"]["described_images"] >= 1
    ref = detail["doc"]["metadata"]["images"][0]
    assert ref.startswith("/media/uploads/")
    assert (tmp_path / "uploads" / ref.rsplit("/", 1)[-1]).is_file()


async def test_upload_pdf_describes_embedded_images(
    client, admin_headers, monkeypatch, tmp_path
):
    """PDF：文字层与内嵌图片一起抽取，图片转描述并入正文、原图落盘存 URL。"""
    import pymupdf

    from app.config import get_settings
    from app.routers import upload as upload_mod

    monkeypatch.setattr(get_settings(), "media_dir", str(tmp_path))

    async def fake_describe(data_url, provider):
        assert data_url.startswith("data:image/")
        return "图中是一张系统架构图，展示了各模块之间的调用关系。"

    monkeypatch.setattr(upload_mod, "_describe_image", fake_describe)

    # 100x100 的图才能通过“小图标过滤”（PDF 路径会带真实尺寸）
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100))
    pix.clear_with(0)
    png = pix.tobytes("png")

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "System architecture overview body text for this document. " * 4)
    page.insert_image(pymupdf.Rect(72, 120, 300, 300), stream=png)
    data = doc.tobytes()
    doc.close()

    resp = await client.post(
        "/api/v1/upload",
        files={"file": ("架构.pdf", data, "application/pdf")},
        data={"title": "架构图文档"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = _ok(resp)
    assert body["status"] == "success", body
    kb = body["knowledge_id"]

    detail = _ok(await client.get(f"/api/v1/ai/knowledge/{kb}", headers=admin_headers))
    assert "architecture overview" in detail["content"]
    assert "系统架构图" in detail["content"]
    assert detail["doc"]["metadata"]["embedded_images"] >= 1
    ref = detail["doc"]["metadata"]["images"][0]
    assert ref.startswith("/media/uploads/")
    assert (tmp_path / "uploads" / ref.rsplit("/", 1)[-1]).is_file()


async def test_rerank_hits_reorders_and_truncates(monkeypatch):
    """精排命中：按 rerank 返回的顺序重排并截断到 keep。"""
    from app.config import get_settings
    from app.services import rag, reranker

    hits = [
        ("d1", "S1", "first text", 0.90),
        ("d2", "S2", "second text", 0.80),
        ("d3", "S3", "third text", 0.70),
        ("d4", "S4", "fourth text", 0.60),
    ]

    async def fake_rerank(query, documents, top_n):
        assert query == "q"
        # 用分片全文参与精排（而非被截断的 snippet）
        assert documents == ["first text", "second text", "third text", "fourth text"]
        return [(2, 0.99), (0, 0.50), (1, 0.10)]

    monkeypatch.setattr(reranker, "rerank", fake_rerank)
    monkeypatch.setattr(get_settings(), "rag_rerank_min_score", 0.0)

    out = await rag._rerank_hits("q", hits, keep=2)
    assert [h[0] for h in out] == ["d3", "d1"]


async def test_rerank_hits_threshold_drops_weak(monkeypatch):
    """精排分均低于阈值：宁缺毋滥，返回空。"""
    from app.config import get_settings
    from app.services import rag, reranker

    hits = [("d1", "S1", "a", 0.9), ("d2", "S2", "b", 0.8), ("d3", "S3", "c", 0.7)]

    async def fake_rerank(query, documents, top_n):
        return [(0, 0.2), (1, 0.1), (2, 0.0)]

    monkeypatch.setattr(reranker, "rerank", fake_rerank)
    monkeypatch.setattr(get_settings(), "rag_rerank_min_score", 0.5)

    out = await rag._rerank_hits("q", hits, keep=2)
    assert out == []


async def test_rerank_hits_falls_back_when_unavailable(monkeypatch):
    """rerank 不可用（返回 None）：回退向量序，不影响可用性。"""
    from app.services import rag, reranker

    async def none_rerank(query, documents, top_n):
        return None

    monkeypatch.setattr(reranker, "rerank", none_rerank)

    hits = [("d1", "S1", "a", 0.9), ("d2", "S2", "b", 0.8), ("d3", "S3", "c", 0.7)]
    out = await rag._rerank_hits("q", hits, keep=2)
    assert [h[0] for h in out] == ["d1", "d2"]


def test_rrf_fuse_accumulates_shared_chunk_and_prefers_cosine():
    """RRF：两腿都命中的分片累加排第一；展示分优先用向量 cosine。"""
    from app.services import rag

    vector_hits = [
        ("d1", "S1", "shared text", 0.90),  # 也在词法榜 rank0
        ("d2", "S2", "only-vector", 0.80),
    ]
    lexical_hits = [
        ("d1", "S1", "shared text", 0.70),
        ("d3", "S3", "only-lexical", 0.60),
    ]
    out = rag._rrf_fuse(
        vector_hits, lexical_hits, k=60, vector_weight=0.5, pool=10
    )
    ids = [h[0] for h in out]
    # d1 两腿累加→第一；d2/d3 分别只在单腿 rank1→并列，按插入序 d2 在前
    assert ids == ["d1", "d2", "d3"]
    # 展示分：d1 用 cosine 0.90（非词法 0.70）；d3 无 cosine→用词法 0.60
    assert out[0][3] == 0.90
    assert out[1][3] == 0.80
    assert out[2][3] == 0.60


def test_rrf_fuse_pool_truncates():
    """融合后的候选池长度受 pool 限制。"""
    from app.services import rag

    vector_hits = [(f"d{i}", f"S{i}", f"t{i}", 1.0 - i / 10) for i in range(5)]
    out = rag._rrf_fuse(
        vector_hits, [], k=60, vector_weight=0.5, pool=2
    )
    assert [h[0] for h in out] == ["d0", "d1"]


def test_rrf_fuse_zero_vector_weight_uses_lexical_only():
    """向量腿权重为 0：只按词法榜融合，向量独有分片不进入结果。"""
    from app.services import rag

    vector_hits = [("d2", "S2", "only-vector", 0.80)]
    lexical_hits = [
        ("d1", "S1", "shared", 0.70),
        ("d3", "S3", "only-lexical", 0.60),
    ]
    out = rag._rrf_fuse(
        vector_hits, lexical_hits, k=60, vector_weight=0.0, pool=10
    )
    assert [h[0] for h in out] == ["d1", "d3"]
