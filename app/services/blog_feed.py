"""RSS 2.0 与 sitemap.xml 的手写 XML 构建（无第三方依赖）。

为什么手写：这两样输出都是「固定形状的 XML 文档」，字段少、结构浅，引一个
feedgen/sitemap 库不值当；且它们要的是 ``application/xml`` 原样字节，本项目的
统一信封中间件只包 ``application/json`` 的 2xx，XML 响应天然原样透传，正好。

调用方（路由）负责把已发布文章取出来、把绝对基址算好，本模块只做数据 → XML。
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import formatdate
from xml.sax.saxutils import escape


def _rfc822(ts: float) -> str:
    """epoch 秒 → RSS 时间（RFC 1123，GMT）。"""
    return formatdate(timeval=ts, localtime=False, usegmt=True)


def _iso8601(ts: float) -> str:
    """epoch 秒 → sitemap 时间（W3C-DTF，UTC，带 Z）。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_rss(
    *,
    title: str,
    link: str,
    description: str,
    language: str,
    items: list[dict],
) -> str:
    """把最近的已发布文章拼成 RSS 2.0 文档字符串。

    ``items`` 每项含 ``slug/title/summary/content/published_at/updated_at``；
    ``link`` 是频道（站点）绝对地址，文章链接按 ``<link>/post/<slug>`` 拼。
    """
    last_build = max((it["updated_at"] for it in items), default=0.0)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">',
        "<channel>",
        f"<title>{escape(title)}</title>",
        f"<link>{escape(link)}</link>",
        f"<description>{escape(description)}</description>",
        f"<language>{escape(language)}</language>",
        f'<atom:link href="{escape(link + "/feed")}" rel="self" type="application/rss+xml"/>',
        f"<lastBuildDate>{_rfc822(last_build)}</lastBuildDate>",
    ]
    for it in items:
        url = f"{link}/post/{it['slug']}"
        summary = it["summary"] or it["content"][:280]
        # 正文原文进 content:encoded，订阅器可展示全文；整段进 CDATA 免转义，
        # 正文里若出现会提前关闭 CDATA 的 "]]>"，按标准拆成两段 CDATA。
        body = it["content"].replace("]]>", "]]]]><![CDATA[>")
        parts += [
            "<item>",
            f"<title>{escape(it['title'])}</title>",
            f"<link>{escape(url)}</link>",
            f'<guid isPermaLink="true">{escape(url)}</guid>',
            f"<pubDate>{_rfc822(it['published_at'])}</pubDate>",
            f"<description>{escape(summary)}</description>",
            f"<content:encoded><![CDATA[{body}]]></content:encoded>",
        ]
        for tag in it.get("tags", []):
            parts.append(f"<category>{escape(tag)}</category>")
        parts.append("</item>")
    parts += ["</channel>", "</rss>"]
    return "\n".join(parts)


def build_sitemap(*, link: str, posts: list[dict], extra: list[dict]) -> str:
    """拼 sitemap.xml：站点首页 + 各文章页 + 分类/标签等附加路径。

    ``posts`` 每项含 ``slug/updated_at``；``extra`` 每项含 ``path/updated_at``
    （path 为相对站点根的路径，如 ``/category/dev``）。
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]

    def _url(path: str, ts: float) -> list[str]:
        loc = f"{link}{path}"
        return [
            "<url>",
            f"<loc>{escape(loc)}</loc>",
            f"<lastmod>{_iso8601(ts)}</lastmod>",
            "</url>",
        ]

    now = max(
        [p["updated_at"] for p in posts] + [e["updated_at"] for e in extra] + [0.0]
    )
    parts += _url("/", now)
    for p in posts:
        parts += _url(f"/post/{p['slug']}", p["updated_at"])
    for e in extra:
        parts += _url(e["path"], e["updated_at"])
    parts.append("</urlset>")
    return "\n".join(parts)


__all__ = ["build_rss", "build_sitemap"]
