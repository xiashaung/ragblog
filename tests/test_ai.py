"""Tests for the AI endpoints (chat, RAG) using the mock backend."""

import json
import time

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


def test_chunk_text_hard_split_prefers_sentence_boundaries():
    """无换行的超长段落切在句末，而不是固定字符宽度截断。"""
    from app.services import rag

    para = "第一步是安装依赖并确认环境变量已配置。" * 60
    chunks = rag.chunk_text(para, chunk_size=300)
    assert len(chunks) > 1
    assert all(0 < len(c) <= 300 for c in chunks)
    # 除最后一块外都停在句末标点上（不再切在半句中间）
    assert all(c.rstrip().endswith("。") for c in chunks[:-1])
    # 拼接后内容不丢：所有片段拼回去能覆盖原文
    assert para.startswith(chunks[0].lstrip())
    assert para.endswith(chunks[-1][-20:])


def test_chunk_text_overlap_scales_with_chunk_size():
    """默认重叠窗口随 chunk_size 缩放，不再固定 80。"""
    from app.services import rag

    text = "".join(f"S{i:05d}" for i in range(800))
    small = rag.chunk_text(text, chunk_size=200)
    large = rag.chunk_text(text, chunk_size=2000)
    assert all(len(c) <= 200 for c in small)
    assert all(len(c) <= 2000 for c in large)
    # 两份都能从第一块头覆盖到最后一块尾
    assert text[-20:] in small[-1] and text[-20:] in large[-1]


def test_split_markdown_sections_tracks_heading_path():
    """标题栈维护层级，代码围栏里的 # 不算标题。"""
    from app.services import rag

    md = (
        "引言正文。\n\n"
        "# 部署指南\n\n概述。\n\n"
        "## Nginx\n\n```\n# 这不是标题\n```\n\n监听 80。\n\n"
        "### 上游配置\n\n后端 8080。\n\n"
        "## Redis\n\n默认端口 6379。"
    )
    sections = rag.split_markdown_sections(md)
    paths = [p for p, _ in sections]
    assert paths == [
        [],
        ["部署指南"],
        ["部署指南", "Nginx"],
        ["部署指南", "Nginx", "上游配置"],
        ["部署指南", "Redis"],
    ]
    # 围栏内的 # 留在了正文里，没被当成标题
    nginx_body = next(b for p, b in sections if p and p[-1] == "Nginx")
    assert "# 这不是标题" in nginx_body


def test_chunk_document_markdown_prepends_heading_path():
    """Markdown 分片带标题路径前缀，不同小节不混进同一片。"""
    from app.services import rag

    md = (
        "# 部署指南\n\n## Nginx 配置\n\n" + "监听 80 端口，开启 gzip。" * 30 + "\n\n"
        "## Redis 配置\n\n默认密码为空。"
    )
    chunks = rag.chunk_document(md, chunk_size=300, fmt="markdown", title="部署指南")
    nginx = [c for c in chunks if "监听" in c]
    redis = [c for c in chunks if "默认密码" in c]
    assert nginx and redis
    assert all(c.startswith("部署指南 > Nginx 配置\n") for c in nginx)
    assert redis[0].startswith("部署指南 > Redis 配置\n")
    # 两个小节的正文不会混进同一片
    assert all("默认密码" not in c for c in nginx)
    # 拼上前缀后仍不超过 chunk_size
    assert all(0 < len(c) <= 300 for c in chunks)


def test_chunk_document_falls_back_for_plain_text_and_headingless():
    """纯文本、以及没有标题的 Markdown 都退化为普通切分（不加前缀）。"""
    from app.services import rag

    text = "问：怎么重启？\n答：执行 systemctl restart。"
    assert rag.chunk_document(text, chunk_size=500, fmt="text", title="FAQ") == [
        text
    ]
    assert rag.chunk_document("没有标题的正文。", chunk_size=500, fmt="markdown", title="X") == [
        "没有标题的正文。"
    ]


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
    assert body["status"] == "indexed"
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


async def test_rag_status_scopes_search(client, admin_headers):
    """检索只命中 ``status == indexed`` 的文档：非已学习状态一律排除。"""
    resp = await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={"title": "A", "content": "Paris is the capital of France. " * 5},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = _ok(resp)
    kb = body["knowledge_id"]
    assert body["status"] == "indexed"

    async def hit_ids(query: str) -> set[str]:
        resp = await client.post(
            "/api/v1/ai/knowledge/search",
            json={"query": query, "top_k": 5},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        return {c["knowledge_id"] for c in _ok(resp)["items"]}

    # 已学习：可被检索
    assert kb in await hit_ids("Paris")

    # 手动把文档打回 pending（模拟切换嵌入模型后待重建），检索应排除它
    from sqlalchemy import update

    from app.db import get_session_factory
    from app.models import KnowledgeDoc

    async with get_session_factory()() as s:
        await s.execute(
            update(KnowledgeDoc).where(KnowledgeDoc.id == kb).values(status="pending")
        )
        await s.commit()

    assert kb not in await hit_ids("Paris")

    # 清理：删掉测试文档
    await client.delete(f"/api/v1/ai/knowledge/{kb}", headers=admin_headers)


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
    # 录入会统一换行并去掉首尾空白，所以字数按 strip 后的正文算
    assert created["chars"] == len(("Paris is the capital of France. " * 30).strip())

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
    # 录入后向量与当前模型一致，直接已学习
    assert row["status"] == "indexed" and row["preview"]
    # 不传 format 的旧写法算纯文本
    assert row["format"] == "text"

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
    assert {p["status"] for p in body["by_status"]} >= {"indexed"}

    # --- reindex rebuilds vectors with the configured provider ---
    resp = await client.post(
        f"/api/v1/ai/knowledge/{kb_id}/reindex", headers=admin_headers
    )
    body = _ok(resp)
    assert body["status"] == "indexed" and body["chunks"] == 1

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


async def test_knowledge_html_ingest_is_normalized_to_markdown(
    client, admin_headers
):
    """富文本录入：HTML 在入口转成 Markdown 才入库，列表/详情都回显 markdown 格式。"""
    html = (
        '<h2>退款说明</h2>'
        '<p>请在<strong>7 天</strong>内申请，详见'
        '<a href="https://a.cn/r">退款页</a>。</p>'
        "<ul><li>保留小票</li><li>原路退回</li></ul>"
        "<table><tr><th>渠道</th><th>时效</th></tr>"
        "<tr><td>支付宝</td><td>1 天</td></tr></table>"
    )
    resp = await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={"title": "退款说明", "content": html, "format": "html"},
        headers=admin_headers,
    )
    created = _ok(resp)
    kb_id = created["knowledge_id"]

    detail = _ok(
        await client.get(f"/api/v1/ai/knowledge/{kb_id}", headers=admin_headers)
    )
    body = detail["content"]
    assert "## 退款说明" in body
    assert "请在**7 天**内申请" in body
    assert "[退款页](https://a.cn/r)" in body
    assert "- 保留小票" in body
    assert "| 渠道 | 时效 |" in body
    # 展示用的标签不该进库：否则分块与向量里全是噪音
    for noise in ("<h2>", "<strong>", "<table>", "<a href"):
        assert noise not in body
    assert detail["doc"]["format"] == "markdown"
    assert detail["doc"]["kind"] == "text"

    # 列表摘要剥掉语法符号，给人看的是文字不是 # 和 |
    rows = _ok(
        await client.get(
            "/api/v1/ai/knowledge",
            params={"keyword": "退款说明"},
            headers=admin_headers,
        )
    )
    item = rows["items"][0]
    assert item["format"] == "markdown"
    assert "#" not in item["preview"] and "<" not in item["preview"]
    assert "退款说明" in item["preview"]

    # 编辑时不传 format 就沿用文档原有格式（只改标题更不会动到正文）
    resp = await client.patch(
        f"/api/v1/ai/knowledge/{kb_id}",
        json={"content": "## 新正文"},
        headers=admin_headers,
    )
    assert _ok(resp)["format"] == "markdown"


def test_html_to_markdown_keeps_block_structure():
    """块级结构转的得对：引用逐行加前缀、代码块保留缩进与语言、列表按缩进嵌套。"""
    from app.services.markdown_text import html_to_markdown

    html = (
        "<blockquote><p>注意</p><p>务必按顺序</p></blockquote>"
        '<pre><code class="language-python">if x:\n    print(1)</code></pre>'
        "<ol><li>外层<script>alert(1)</script><ul><li>内层</li></ul></li></ol>"
        "<p>分隔线之前</p><hr><p><del>作废</del>与<u>下划线</u></p>"
    )
    md = html_to_markdown(html)
    assert "> 注意\n>\n> 务必按顺序" in md
    assert "```python\nif x:\n    print(1)\n```" in md
    assert "1. 外层\n  - 内层" in md
    assert "alert" not in md and "---" in md
    assert "~~作废~~" in md
    # 没有 Markdown 对应的语义（下划线）只留文字，不编造语法
    assert "<u>" not in md and "下划线" in md


async def test_knowledge_html_without_body_is_rejected(client, admin_headers):
    """只有样式标签的 HTML 转完是空的：直接 400，别留一篇搜不到的空文档。"""
    resp = await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={"title": "空壳", "content": "<div></div><span></span>", "format": "html"},
        headers=admin_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "归一化后为空" in resp.json()["message"]


async def test_upload_image_returns_media_url_without_ingesting(client, admin_headers):
    """编辑器插图：只落盘返地址，不建文档、也不要视觉模型。"""
    resp = await client.post(
        "/api/v1/upload/image",
        files={"file": ("screenshot.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        headers=admin_headers,
    )
    data = _ok(resp)
    assert data["url"].startswith("/media/uploads/") and data["url"].endswith(".png")
    assert data["name"] == "screenshot.png" and data["bytes"] > 0

    # 图片只是正文的一部分，不该因此多出一篇文档
    rows = _ok(await client.get("/api/v1/ai/knowledge", headers=admin_headers))
    assert all("screenshot" not in item["title"] for item in rows["items"])

    # 粘贴进来的可能根本不是支持的格式：直接 400，不往媒体目录写脏文件
    resp = await client.post(
        "/api/v1/upload/image",
        files={"file": ("diagram.svg", b"<svg></svg>", "image/svg+xml")},
        headers=admin_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "不支持的图片格式" in resp.json()["message"]


async def test_upload_image_creates_vector_and_is_searchable(client, admin_headers):
    """图片入库两者共存：文字描述进 chunk，图片本体进 ``knowledge_images`` 并可文搜图。"""
    from sqlalchemy import select

    from app.config import get_settings
    from app.db import get_session_factory
    from app.models import KnowledgeImage

    resp = await client.post(
        "/api/v1/upload",
        files={"file": ("cat.png", b"\x89PNG\r\n\x1a\nmeow-bytes", "image/png")},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = _ok(resp)
    assert body["status"] == "success"
    kb = body["knowledge_id"]

    # 图片向量行落库：一张一行，维度对齐 ``rag_image_vector_dim``
    image_dim = get_settings().rag_image_vector_dim
    async with get_session_factory()() as s:
        rows = (
            await s.scalars(select(KnowledgeImage).where(KnowledgeImage.knowledge_id == kb))
        ).all()
    assert len(rows) == 1
    assert rows[0].vector is not None and len(rows[0].vector) == image_dim
    assert rows[0].image_url.startswith("/media/uploads/")

    # 文搜图：查询文本进 VL 空间后能召回这张图（mock 下无语义，只验链路）
    hits = _ok(
        await client.post(
            "/api/v1/ai/knowledge/search-images",
            json={"query": "一张猫的图", "top_k": 5},
            headers=admin_headers,
        )
    )
    assert hits["provider"] == "mock"
    assert {h["knowledge_id"] for h in hits["items"]} == {kb}

    # 删文档级联清掉图片向量行（不留孤儿）
    await client.delete(f"/api/v1/ai/knowledge/{kb}", headers=admin_headers)
    async with get_session_factory()() as s:
        left = (
            await s.scalars(select(KnowledgeImage).where(KnowledgeImage.knowledge_id == kb))
        ).all()
    assert left == []


async def test_image_vector_failure_keeps_text_chunks(client, admin_headers, monkeypatch):
    """图片向量失败（维度不符）不阻断文本入库：文档仍 ``indexed``，只记 ``image_index_error``。"""
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.models import KnowledgeDoc, KnowledgeImage
    from app.services import rag

    # 把图片列维度改成一个 mock 给不出的值：embed_images 仍返 rag_image_vector_dim 长向量，
    # 与 64 不符 → 写向量时报错，但被 SAVEPOINT 兜住，文本 chunk 不受牵连。
    monkeypatch.setattr(rag, "_image_vector_dim", lambda: 64)

    resp = await client.post(
        "/api/v1/upload",
        files={"file": ("err.png", b"\x89PNG\r\n\x1a\nbytes", "image/png")},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = _ok(resp)
    assert body["status"] == "success"  # 文本入库成功
    kb = body["knowledge_id"]

    async with get_session_factory()() as s:
        doc = await s.get(KnowledgeDoc, kb)
        imgs = (
            await s.scalars(select(KnowledgeImage).where(KnowledgeImage.knowledge_id == kb))
        ).all()
        status_value = doc.status
        image_error = (doc.meta or {}).get("image_index_error")
    assert status_value == "indexed"  # 状态不因图片失败而翻转
    assert imgs == []  # 维度不符的图片向量被回滚
    assert image_error and "64" in image_error

    await client.delete(f"/api/v1/ai/knowledge/{kb}", headers=admin_headers)


async def test_image_embedding_provider_change_reindexes_images(client, admin_headers):
    """改 ``image_embedding_provider`` 触发图片向量全库重建（同步模式直接跑完）。"""
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.models import KnowledgeImage

    resp = await client.post(
        "/api/v1/upload",
        files={"file": ("dog.png", b"\x89PNG\r\n\x1a\nbark-bytes", "image/png")},
        headers=admin_headers,
    )
    kb = _ok(resp)["knowledge_id"]

    async def image_count() -> int:
        async with get_session_factory()() as s:
            return len(
                (
                    await s.scalars(
                        select(KnowledgeImage).where(KnowledgeImage.knowledge_id == kb)
                    )
                ).all()
            )

    assert await image_count() == 1

    resp = await client.patch(
        "/api/v1/system/config/image_embedding_provider",
        json={"value": "deepseek"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    # 重建后图片向量仍在（确定性 mock，行数不变），且文档仍可被文搜图
    assert await image_count() == 1
    hits = _ok(
        await client.post(
            "/api/v1/ai/knowledge/search-images",
            json={"query": "狗", "top_k": 5},
            headers=admin_headers,
        )
    )
    assert kb in {h["knowledge_id"] for h in hits["items"]}

    await client.delete(f"/api/v1/ai/knowledge/{kb}", headers=admin_headers)


async def test_knowledge_status_filter_and_full_rebuild(client, admin_headers):
    """列表可按状态过滤；全库重建把文档拉回 indexed；换嵌入模型触发重建。"""
    from sqlalchemy import update

    from app.db import get_session_factory
    from app.models import KnowledgeDoc

    ids: list[str] = []
    for title in ("DocX", "DocY"):
        resp = await client.post(
            "/api/v1/ai/knowledge/ingest",
            json={"title": title, "content": f"{title} is a capital city. " * 5},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert _ok(resp)["status"] == "indexed"
        ids.append(_ok(resp)["knowledge_id"])

    # 按 status=indexed 过滤：两篇都在
    items = _ok(
        await client.get(
            "/api/v1/ai/knowledge", params={"status": "indexed"}, headers=admin_headers
        )
    )["items"]
    assert set(ids) <= {i["knowledge_id"] for i in items}

    # 手动把 DocX 打回 pending，可按 status=pending 只筛出它
    async with get_session_factory()() as s:
        await s.execute(
            update(KnowledgeDoc)
            .where(KnowledgeDoc.id == ids[0])
            .values(status="pending")
        )
        await s.commit()
    items = _ok(
        await client.get(
            "/api/v1/ai/knowledge", params={"status": "pending"}, headers=admin_headers
        )
    )["items"]
    assert {i["knowledge_id"] for i in items} == {ids[0]}

    # 全库重建（同步模式直接跑完）：pending → indexed
    resp = await client.post("/api/v1/ai/knowledge/reindex-all", headers=admin_headers)
    assert resp.status_code == 200
    assert _ok(resp)["enqueued"] >= 1
    items = _ok(
        await client.get(
            "/api/v1/ai/knowledge", params={"status": "indexed"}, headers=admin_headers
        )
    )["items"]
    assert ids[0] in {i["knowledge_id"] for i in items}

    # 切换嵌入模型：钩子自动触发全库重建，重建后文档仍已学习且可检索
    resp = await client.patch(
        "/api/v1/system/config/embedding_provider",
        json={"value": "deepseek"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    hits = _ok(
        await client.post(
            "/api/v1/ai/knowledge/search",
            json={"query": "capital", "top_k": 10},
            headers=admin_headers,
        )
    )["items"]
    assert ids[0] in {h["knowledge_id"] for h in hits}

    for kid in ids:
        await client.delete(f"/api/v1/ai/knowledge/{kid}", headers=admin_headers)


async def test_mark_docs_for_full_rebuild_sets_failed_without_content(
    client, admin_headers
):
    """无原文的老文档无法重建：全库标记时直接置 failed 并写原因。"""
    from app.db import get_session_factory
    from app.models import KnowledgeDoc
    from app.services import rag

    # 造一篇无原文的文档（content=None）
    async with get_session_factory()() as s:
        doc = KnowledgeDoc(title="无原文老档", content=None, status="indexed")
        s.add(doc)
        await s.commit()
        doc_id = doc.id

    async with get_session_factory()() as s:
        affected = await rag.mark_docs_for_full_rebuild(s)
        await s.commit()
    assert affected >= 1

    row = _ok(
        await client.get(f"/api/v1/ai/knowledge/{doc_id}", headers=admin_headers)
    )["doc"]
    assert row["status"] == "failed"
    assert "无原文" in (row["reindex_error"] or "")

    await client.delete(f"/api/v1/ai/knowledge/{doc_id}", headers=admin_headers)


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


async def test_rag_task_persisted_and_queryable(client, admin_headers):
    """RAG 入库任务：登记行落库 → 同步执行 → 状态端点可查。"""
    from app import db
    from app.services import rag_queue

    task = rag_queue.RAGTask(
        task_type=rag_queue.TaskType.INGEST,
        payload={
            "title": "持久化测试",
            "content": "段落一内容足够长可以切片。" * 10,
            "format": "text",
        },
        created_at=time.time(),
    )
    async with db.session_factory() as session:
        task_id = await rag_queue.get_queue().enqueue(session, task, sync=True)
    # 查询单个任务状态
    resp = await client.get(
        f"/api/v1/ai/knowledge/tasks/{task_id}", headers=admin_headers
    )
    assert resp.status_code == 200
    data = _ok(resp)
    assert data["status"] == "success"
    assert data["result"]["chunks"] >= 1
    kb_id = data["knowledge_id"]
    assert kb_id is not None
    # 验证文档确实入库了
    detail = _ok(await client.get(f"/api/v1/ai/knowledge/{kb_id}", headers=admin_headers))
    assert detail["doc"]["title"] == "持久化测试"


async def test_rag_task_status_404_for_unknown(client, admin_headers):
    """未知 task_id 返回 404。"""
    resp = await client.get(
        "/api/v1/ai/knowledge/tasks/rag-nonexistent", headers=admin_headers
    )
    assert resp.status_code == 404


async def test_rag_task_list_pagination(client, admin_headers):
    """任务列表分页：总数、offset/limit 生效。"""
    from app import db
    from app.services import rag_queue

    for i in range(5):
        task = rag_queue.RAGTask(
            task_type=rag_queue.TaskType.INGEST,
            payload={"title": f"任务{i}", "content": "a" * 100, "format": "text"},
            created_at=time.time() + i,
        )
        async with db.session_factory() as session:
            await rag_queue.get_queue().enqueue(session, task, sync=True)

    # 总行数应为 5
    resp = await client.get("/api/v1/ai/knowledge/tasks", headers=admin_headers)
    assert resp.status_code == 200
    data = _ok(resp)
    assert data["total"] == 5
    assert len(data["items"]) == 5

    # offset/limit 生效
    resp = await client.get(
        "/api/v1/ai/knowledge/tasks?offset=2&limit=2", headers=admin_headers
    )
    assert resp.status_code == 200
    data = _ok(resp)
    assert data["total"] == 5
    assert len(data["items"]) == 2
    # 按创建时间倒序，所以是 [4,3]
    assert data["items"][0]["task_id"] != data["items"][1]["task_id"]
