"""博客 RSS / sitemap：XML 构建的合法性 + 公开端点端到端。

单元层用 ``xml.etree`` 反解构建结果——能解析即证明转义 / CDATA / 命名空间都没写坏；
API 层发一篇再拉 /feed 与 /sitemap.xml，验 content-type 与链接形状。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from http import HTTPStatus

from app.services.blog_feed import build_rss, build_sitemap
from tests.conftest import payload

SMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"


# ---------------- 单元：XML 构建 ----------------


def test_build_rss_is_wellformed_and_escapes() -> None:
    items = [
        {
            "slug": "hello",
            "title": "A & B <tag>",
            "summary": "x < y",
            "content": "正文含 ]]> 需拆分",
            "tags": ["t1", "t2"],
            "published_at": 1_700_000_000.0,
            "updated_at": 1_700_000_001.0,
        }
    ]
    xml = build_rss(
        title="站点 & 名", link="https://x.com", description="desc", language="zh-CN", items=items
    )
    # 能反解 = 转义与 CDATA 拆分都合法
    root = ET.fromstring(xml)
    item = root.find("./channel/item")
    assert item is not None
    assert item.findtext("title") == "A & B <tag>"
    assert item.findtext("link") == "https://x.com/post/hello"
    assert item.findtext(f"{CONTENT_NS}encoded") == "正文含 ]]> 需拆分"
    assert [c.text for c in item.findall("category")] == ["t1", "t2"]


def test_build_sitemap_lists_home_posts_and_extra() -> None:
    xml = build_sitemap(
        link="https://x.com",
        posts=[{"slug": "a", "updated_at": 1_700_000_000.0}],
        extra=[{"path": "/category/dev", "updated_at": 1_700_000_000.0}],
    )
    root = ET.fromstring(xml)
    locs = [u.findtext(f"{SMAP_NS}loc") for u in root.findall(f"{SMAP_NS}url")]
    assert locs == ["https://x.com/", "https://x.com/post/a", "https://x.com/category/dev"]


# ---------------- API：公开端点端到端 ----------------


async def _publish_one(client, h) -> str:
    resp = await client.post(
        "/api/v1/admin/blogs",
        json={"title": "订阅测试文章", "content": "# Hi\n正文 & 特殊字符"},
        headers=h,
    )
    assert resp.status_code == HTTPStatus.CREATED, resp.text
    pid = payload(resp)["id"]
    resp = await client.post(f"/api/v1/admin/blogs/{pid}/publish", headers=h)
    assert resp.status_code == HTTPStatus.OK, resp.text
    return payload(resp)["slug"]


async def test_feed_endpoint_returns_raw_rss(client, console) -> None:
    slug = await _publish_one(client, console["headers"])
    resp = await client.get("/api/v1/blog/feed")
    assert resp.status_code == HTTPStatus.OK
    assert resp.headers["content-type"].startswith("application/rss+xml")
    # 原样 XML，未被统一信封包裹
    assert resp.text.lstrip().startswith("<?xml")
    root = ET.fromstring(resp.text)
    links = [item.findtext("link") for item in root.findall("./channel/item")]
    assert any(link.endswith(f"/post/{slug}") for link in links)


async def test_sitemap_endpoint_lists_published_post(client, console) -> None:
    slug = await _publish_one(client, console["headers"])
    resp = await client.get("/api/v1/blog/sitemap.xml")
    assert resp.status_code == HTTPStatus.OK
    assert resp.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(resp.text)
    locs = [u.findtext(f"{SMAP_NS}loc") for u in root.findall(f"{SMAP_NS}url")]
    assert any(loc.endswith(f"/post/{slug}") for loc in locs)


async def test_feed_empty_when_no_published(client) -> None:
    """没有已发布文章：feed 仍是合法 RSS，channel 下零 item。"""
    resp = await client.get("/api/v1/blog/feed")
    assert resp.status_code == HTTPStatus.OK
    root = ET.fromstring(resp.text)
    assert root.findall("./channel/item") == []
