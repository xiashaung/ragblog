"""抓一个网页并把它压成纯文本，供知识库录入。

刻意只用标准库（``html.parser``）+ 项目已有的 ``httpx``，不为了「录入一个 URL」
再拖一个 HTML 解析包进来。抽取器保留文档的阅读顺序，只丢掉导航/页脚/脚本/表单
这些外围（正是 RAG 分块器想要的东西）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

MAX_DOWNLOAD_BYTES = 3_000_000  # 单次下载上限，超过直接拒入库
DEFAULT_TIMEOUT = 15.0  # 宁可放弃一次抓取，也不让用户干等
USER_AGENT = "Mozilla/5.0 (compatible; FastAPI-KnowledgeBot/0.1)"

_ALLOWED_SCHEMES = {"http", "https"}

# 这些标签的子树永远不算「可读正文」
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

# 这些标签会吐一个换行，让段落各自成行（RAG 分块器优先按换行切）
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

# 一行至少得含一个字母/数字（含中日韩文字）才保留，
# 以此丢掉装饰性分隔线和空表格边框
_HAS_WORD = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_SPACES = re.compile(r"[ \t\r\f\v\u3000]+")


class _Extractor(HTMLParser):
    """按文档顺序收集 ``<title>`` 与可见正文。"""

    def __init__(self) -> None:
        """标题与正文分开存；``_skip_depth`` 是跳过的嵌套深度。"""
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """进标签：title 起标记，屏蔽类加深计数，块类补一个换行。"""
        if tag == "title":
            self._in_title = True
            return
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """出标签：与 :meth:`handle_starttag` 一一对称，计数不会跑到负数。"""
        if tag == "title":
            self._in_title = False
            return
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        """文本节点：在 title 里归标题，在屏蔽子树里直接丢。"""
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth:
            return
        self.text_parts.append(data)


def _clean_line(line: str) -> str:
    """把一行里的连续空白（含全角空格）压成单空格并去两端。"""
    return _SPACES.sub(" ", line).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """返一份 HTML 文档的 ``(标题, 正文)``。

    ``正文`` 一行一段，所以 :func:`app.services.rag.chunk_text` 能按段落边界打包。
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
    """一次抓页的结果：请求地址与重定向后的终态地址都保留，方便回溯来源。"""

    url: str
    final_url: str
    status: int
    title: str
    text: str
    truncated: bool = False


def validate_url(url: str) -> str:
    """只放过普通的 http(s) 地址（挡掉 ``file://`` 等奇怪 scheme）。"""
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
    """下载 ``url`` 并抽取它的可读正文。

    预期内的失败（scheme 不对、网络错、非 2xx、内容不是文本、体积超限、正文为空）
    一律抛 ``ValueError`` + 中文描述，由全局处理器翻译成 HTTP 400。
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
    else:  # 纯文本 / markdown 直接拿来用
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
