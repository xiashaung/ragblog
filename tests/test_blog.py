"""博客模块：后台 CRUD 与 RBAC、发布同步知识库、前台公开查询与 AI 检索。

约定与相邻测试一致：mock provider 全量打桩（不联网），``APP_TEST_MODE`` 下
录入同步执行，因此发布动作会在请求内真正把分片向量落库。
"""

from __future__ import annotations

import json
from http import HTTPStatus

from tests.conftest import payload


async def _create_post(client, headers, **overrides) -> dict:
    """建一篇默认标题为「缓存实践笔记」的文章，返回响应体。"""
    body = {
        "title": "缓存实践笔记",
        "content": "# 缓存\nRedis 的过期策略包括惰性删除与定期删除。",
        "tags": ["redis", "后端"],
        **overrides,
    }
    resp = await client.post("/admin/blogs", json=body, headers=headers)
    assert resp.status_code == HTTPStatus.CREATED, resp.text
    return payload(resp)


async def test_blog_crud_and_publish_roundtrip(client, console):
    """草稿 → 发布（进索引）→ 前台可见 → 下线（摘索引）→ 删除。"""
    h = console["headers"]
    post = await _create_post(client, h)
    assert post["status"] == "draft"
    # 中文标题剥不成 ASCII slug → 服务层随机兜底（post-xxxxxx）
    assert post["slug"].startswith("post-")
    pid = post["id"]

    # 草稿对前台不可见
    resp = await client.get("/api/v1/blog/posts")
    assert payload(resp)["total"] == 0

    resp = await client.post(f"/admin/blogs/{pid}/publish", headers=h)
    assert resp.status_code == HTTPStatus.OK, resp.text
    published = payload(resp)
    assert published["status"] == "published"
    assert published["knowledge_id"], "发布应同步出知识文档 ID"

    resp = await client.get("/api/v1/blog/posts")
    items = payload(resp)["items"]
    assert [i["slug"] for i in items] == [published["slug"]]
    assert "id" not in items[0] and "knowledge_id" not in items[0]

    resp = await client.get(f"/api/v1/blog/posts/{published['slug']}")
    detail = payload(resp)
    assert "Redis" in detail["content"]
    first_views = detail["view_count"]
    resp = await client.get(f"/api/v1/blog/posts/{published['slug']}")
    assert payload(resp)["view_count"] == first_views + 1

    resp = await client.get("/api/v1/blog/tags")
    names = {t["name"] for t in payload(resp)["items"]}
    assert names == {"redis", "后端"}

    # 下线后前台与检索都应当清空
    resp = await client.post(f"/admin/blogs/{pid}/unpublish", headers=h)
    assert payload(resp)["status"] == "draft"
    assert payload(await client.get("/api/v1/blog/posts"))["total"] == 0
    resp = await client.post("/api/v1/blog/search", json={"query": "过期策略"})
    assert payload(resp)["items"] == []

    resp = await client.delete(f"/admin/blogs/{pid}", headers=h)
    assert resp.status_code == HTTPStatus.NO_CONTENT
    assert payload(await client.get("/admin/blogs", headers=h))["total"] == 0


async def test_publish_rejects_empty_content(client, console):
    """空正文发布 → 400（服务层 ValueError 由全局处理器翻译）。"""
    h = console["headers"]
    post = await _create_post(client, h, content="")
    resp = await client.post(f"/admin/blogs/{post['id']}/publish", headers=h)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "正文为空" in resp.json()["message"]


async def test_blog_rbac_gating(client, console, account_factory):
    """RBAC：operator 无权（403）；viewer 可读列表但不能建文章。"""
    operator = await account_factory("op1", ["operator"])
    resp = await client.get("/admin/blogs", headers=operator["headers"])
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert "blog:read" in resp.json()["message"] or "查看博客" in resp.json()["message"]

    viewer = await account_factory("view1", ["viewer"])
    resp = await client.get("/admin/blogs", headers=viewer["headers"])
    assert resp.status_code == HTTPStatus.OK, resp.text  # blog:read 已随内置角色预发
    resp = await client.post(
        "/admin/blogs", json={"title": "x", "content": "y"}, headers=viewer["headers"]
    )
    assert resp.status_code == HTTPStatus.FORBIDDEN
    # 注：不在这里断言「不带 header 应 401」——本 client 的登录响应往 cookie 里
    # 种过 admin_token，httpx 会自动带着它过鉴权（见 test_rbac 里的同一坑）。


async def test_blog_search_scoped_to_published_posts(client, console, admin_headers):
    """隔离性：知识库里的普通文档不会混进博客搜索；改标题重发布不残留旧索引。"""
    h = console["headers"]
    # 先往知识库塞一篇与博客无关的文档
    resp = await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={"title": "员工手册", "content": "年假申请流程说明文档。"},
        headers=admin_headers,
    )
    assert resp.status_code == HTTPStatus.OK, resp.text
    kb_id = payload(resp)["knowledge_id"]

    post = await _create_post(client, h)
    pid = post["id"]
    published = payload(
        await client.post(f"/admin/blogs/{pid}/publish", headers=h)
    )
    old_doc = published["knowledge_id"]

    resp = await client.post(
        "/api/v1/blog/search", json={"query": "缓存的过期策略", "top_k": 5}
    )
    hits = payload(resp)["items"]
    assert hits and all(item["slug"] == published["slug"] for item in hits)

    # 改标题再发布：标题派生的旧文档要被删掉，不残留
    resp = await client.patch(
        f"/admin/blogs/{pid}", json={"title": "缓存实践手册"}, headers=h
    )
    assert resp.status_code == HTTPStatus.OK  # 已发布文章改标题会自动重同步
    new_doc = payload(resp)["knowledge_id"]
    assert new_doc and new_doc != old_doc
    resp = await client.get(f"/api/v1/ai/knowledge/{old_doc}", headers=admin_headers)
    assert resp.status_code == HTTPStatus.NOT_FOUND

    # 非博客文档仍安然无恙；检索结果永远只出博客文章（mock 向量无语义，
    # 隔离性看的是「命中的集合里没有员工手册」而不是相关性得分）
    resp = await client.get(f"/api/v1/ai/knowledge/{kb_id}", headers=admin_headers)
    assert resp.status_code == HTTPStatus.OK
    resp = await client.post("/api/v1/blog/search", json={"query": "年假申请流程"})
    assert all(i["slug"] == published["slug"] for i in payload(resp)["items"])

    # 删除博客文章连带删掉同步出的知识文档
    resp = await client.delete(f"/admin/blogs/{pid}", headers=h)
    assert resp.status_code == HTTPStatus.NO_CONTENT
    resp = await client.get(f"/api/v1/ai/knowledge/{new_doc}", headers=admin_headers)
    assert resp.status_code == HTTPStatus.NOT_FOUND


async def test_blog_answer_no_hits_skips_model(client, console):
    """没有任何已发布文章时：问答不调模型，直接给「未找到」文案。"""
    resp = await client.post("/api/v1/blog/answer", json={"query": "有没有文章"})
    body = payload(resp)
    assert body["citations"] == []
    assert "未找到" in body["answer"]


async def test_blog_chat_streams_citations_then_chunks(client, console):
    """对话流：NDJSON 第一行带引用文章，正文随后，末行 done（mock 全链路可测）。"""
    h = console["headers"]
    post = await _create_post(client, h)
    published = payload(
        await client.post(f"/admin/blogs/{post['id']}/publish", headers=h)
    )

    resp = await client.post(
        "/api/v1/blog/chat",
        json={"messages": [{"role": "user", "content": "缓存过期策略是什么"}]},
    )
    assert resp.status_code == HTTPStatus.OK
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    chunks = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    assert chunks, "流不能是空的"
    # 第一行：本轮检索引用（slug 可跳转）
    assert chunks[0]["citations"], chunks[0]
    assert chunks[0]["citations"][0]["slug"] == published["slug"]
    content = "".join(c["content"] for c in chunks)
    assert content  # mock 回显会带上本轮提问
    assert chunks[-1]["done"] is True
    assert not any(c.get("error") for c in chunks)


async def test_blog_chat_rejects_bad_requests(client, console):
    """公开对话的入参门：非 user 结尾 / 超长消息都会被 422 挡下。"""
    resp = await client.post(
        "/api/v1/blog/chat",
        json={"messages": [{"role": "assistant", "content": "hi"}]},
    )
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    resp = await client.post(
        "/api/v1/blog/chat",
        json={"messages": [{"role": "user", "content": "x" * 8001}]},
    )
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def _create_category(client, h, **overrides) -> dict:
    """建一个默认名为「Tech」的分类，返回响应体。"""
    body = {"name": "Tech", "sort_order": 1, **overrides}
    resp = await client.post("/admin/blogs/categories", json=body, headers=h)
    assert resp.status_code == HTTPStatus.CREATED, resp.text
    return payload(resp)


async def test_blog_category_crud(client, console):
    """分类：slug 自动生成/兜底、重名拦截、排序、空了才能删。"""
    h = console["headers"]
    cat = await _create_category(client, h)
    assert cat["slug"] == "tech"  # 英文名直接生成 slug
    assert cat["post_count"] == 0

    # 重名 → 400
    resp = await client.post(
        "/admin/blogs/categories", json={"name": "Tech"}, headers=h
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "已被占用" in resp.json()["message"]

    # 纯中文名剥不出 ASCII slug → 随机兜底
    zh = await _create_category(client, h, name="技术")
    assert zh["slug"].startswith("cat-")

    # 改名 + 改排序
    resp = await client.patch(
        f"/admin/blogs/categories/{cat['id']}",
        json={"name": "Backend", "sort_order": 5},
        headers=h,
    )
    assert resp.status_code == HTTPStatus.OK
    body = payload(resp)
    assert body["name"] == "Backend"
    assert body["sort_order"] == 5

    # 列表按 sort_order 排：Backend(5) 在 技术 之后还是之前？默认 0 < 5
    items = payload(await client.get("/admin/blogs/categories", headers=h))["items"]
    assert [c["name"] for c in items] == ["技术", "Backend"]

    resp = await client.delete(f"/admin/blogs/categories/{zh['id']}", headers=h)
    assert resp.status_code == HTTPStatus.NO_CONTENT
    resp = await client.delete(f"/admin/blogs/categories/{zh['id']}", headers=h)
    assert resp.status_code == HTTPStatus.NOT_FOUND


async def test_post_category_assignment_and_public_filter(client, console):
    """文章归属分类：后台校验/回显，前台按 slug 筛选且分类导航只出有货的。"""
    h = console["headers"]
    cat = await _create_category(client, h)

    # 挂不存在的分类 → 400
    resp = await client.post(
        "/admin/blogs",
        json={"title": "x", "content": "y", "category_id": "bc-nope"},
        headers=h,
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "分类不存在" in resp.json()["message"]

    post = await _create_post(client, h, category_id=cat["id"])
    assert post["category"] == {"name": "Tech", "slug": "tech"}

    # 后台按分类 ID 筛选
    listing = payload(
        await client.get(f"/admin/blogs?category_id={cat['id']}", headers=h)
    )
    assert listing["total"] == 1
    other = await _create_category(client, h, name="Life")
    assert (
        payload(await client.get(f"/admin/blogs?category_id={other['id']}", headers=h))[
            "total"
        ]
        == 0
    )

    # 草稿不进分类计数；发布后才出现在前台分类导航
    resp = await client.post(f"/admin/blogs/{post['id']}/publish", headers=h)
    published = payload(resp)
    assert published["category"]["slug"] == "tech"
    cats = payload(await client.get("/api/v1/blog/categories"))["items"]
    assert [(c["name"], c["post_count"]) for c in cats] == [("Tech", 1)]
    assert "id" not in cats[0]  # 内部 ID 不外泄

    # 前台按分类 slug 筛选；乱写的 slug 给空页不报错
    hit = payload(await client.get("/api/v1/blog/posts?category=tech"))
    assert hit["total"] == 1
    assert hit["items"][0]["category"] == {"name": "Tech", "slug": "tech"}
    assert "category_id" not in hit["items"][0]
    assert payload(await client.get("/api/v1/blog/posts?category=nope"))["items"] == []

    # 文章详情页带分类摘要
    detail = payload(await client.get(f"/api/v1/blog/posts/{published['slug']}"))
    assert detail["category"]["slug"] == "tech"

    # 分类下有文章时删除被拦；移出文章后可以删掉
    resp = await client.delete(f"/admin/blogs/categories/{cat['id']}", headers=h)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "请先重新归类" in resp.json()["message"]
    resp = await client.patch(
        f"/admin/blogs/{post['id']}", json={"category_id": None}, headers=h
    )
    assert payload(resp)["category"] is None
    resp = await client.delete(f"/admin/blogs/categories/{cat['id']}", headers=h)
    assert resp.status_code == HTTPStatus.NO_CONTENT
