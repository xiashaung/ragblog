"""Fetch a web page and reduce it to plain text for knowledge ingestion.

Deliberately stdlib-only (``html.parser``) plus ``httpx`` (already a project
dependency) so "录入一个 URL" does not drag in an HTML-parsing package. The
extractor keeps the reading order of the document and drops chrome (nav,
footer, scripts, forms), which is what the RAG chunker wants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

MAX_DOWNLOAD_BYTES = 3_000_000
DEFAULT_TIMEOUT = 15.0
USER_AGENT = "Mozilla/5.0 (compatible; FastAPI-KnowledgeBot/0.1)"

_ALLOWED_SCHEMES = {"http", "https"}

# Subtree contents of these tags are never part of the readable text.
_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "head",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "button",
    "select",
    "iframe",
    "object",
    "embed",
    "video",
    "audio",
}

# These emit a line break so paragraphs survive as separate lines (the RAG
# chunker splits on newlines first).
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "caption",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}

# A line must contain at least one letter/digit (CJK included) to be kept,
# which drops decorative separators and empty table borders.
_HAS_WORD = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_SPACES = re.compile(r"[ \t\r\f\v\u3000]+")


class _Extractor(HTMLParser):
    """Collect ``<title>`` and the visible text, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth:
            return
        self.text_parts.append(data)


def _clean_line(line: str) -> str:
    return _SPACES.sub(" ", line).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Return ``(title, text)`` for an HTML document.

    ``text`` has one paragraph per line, so :func:`app.services.rag.chunk_text`
    can pack it at paragraph boundaries.
    """
    parser = _Extractor()
    parser.feed(html)
    parser.close()

    title = _clean_line("".join(parser.title_parts))
    lines = (_clean_line(line) for line in "".join(parser.text_parts).split("\n"))
    kept = [line for line in lines if line and _HAS_WORD.search(line)]
    return title, "\n".join(kept)


@dataclass
class PageText:
    url: str
    final_url: str
    status: int
    title: str
    text: str
    truncated: bool = False


def validate_url(url: str) -> str:
    """Reject anything that is not a plain http(s) address."""
    target = url.strip()
    parsed = urlparse(target)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError("仅支持 http / https 地址")
    if not parsed.netloc:
        raise ValueError("网址缺少域名")
    return target


async def fetch_page(
    url: str, *, max_chars: int | None = None, timeout: float = DEFAULT_TIMEOUT
) -> PageText:
    """Download *url* and extract its readable text.

    Raises ``ValueError`` (surfaced as HTTP 400 by the app's handler) with a
    Chinese message for every expected failure: bad scheme, network error,
    non-2xx response, non-textual content type, oversized body, empty text.
    """
    target = validate_url(url)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(target)
    except httpx.HTTPError as exc:
        raise ValueError(f"抓取失败：{exc}") from exc

    if resp.status_code >= 400:
        raise ValueError(f"页面返回 HTTP {resp.status_code}，无法入库")
    if len(resp.content) > MAX_DOWNLOAD_BYTES:
        raise ValueError(
            f"页面过大（{len(resp.content)} 字节），上限 {MAX_DOWNLOAD_BYTES} 字节"
        )

    ctype = resp.headers.get("content-type", "").lower()
    if ctype and not any(kind in ctype for kind in ("html", "xml", "text", "json")):
        raise ValueError(f"链接不是网页或文本内容（{ctype.split(';')[0]}）")

    body = resp.text
    if "html" in ctype or "xml" in ctype or "<html" in body[:2000].lower():
        title, text = html_to_text(body)
    else:  # plain text / markdown served as-is
        title, text = "", _clean_line(body.replace("\r\n", "\n").replace("\r", "\n"))
        text = "\n".join(
            line for line in text.split("\n") if line and _HAS_WORD.search(line)
        )

    if len(text.strip()) < 10:
        raise ValueError("页面没有可入库的正文（可能是动态渲染或需要登录）")

    truncated = False
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    return PageText(
        url=target,
        final_url=str(resp.url),
        status=resp.status_code,
        title=title,
        text=text,
        truncated=truncated,
    )
