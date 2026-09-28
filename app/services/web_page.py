"""抓一个网页并把它压成纯文本，供知识库录入。

刻意只用标准库（``html.parser``）+ 项目已有的 ``httpx``，不为了「录入一个 URL」
再拖一个 HTML 解析包进来。抽取器保留文档的阅读顺序，只丢掉导航/页脚/脚本/表单
这些外围（正是 RAG 分块器想要的东西）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from ..config import get_settings

MAX_DOWNLOAD_BYTES = 3_000_000  # 单次下载上限，超过直接拒入库
DEFAULT_TIMEOUT = 15.0  # 宁可放弃一次抓取，也不让用户干等
USER_AGENT = "Mozilla/5.0 (compatible; FastAPI-KnowledgeBot/0.1)"

_ALLOWED_SCHEMES = {"http", "https"}

# 内网/元数据服务常用的主机名与后缀：IP 字面量之外这些也要在入口挡掉
_DENY_HOSTS = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "nvme1.local",
}
_DENY_SUFFIXES = (".local", ".internal", ".lan", ".cluster.local")
_MAX_REDIRECTS = 5  # 手动跟随重定向的跳数上限，防循环跳转

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


def ip_denied(ip: str) -> bool:
    """判断一个 IP 是否属于「不该被服务端去访问」的地址段。

    含：私网（10/8、172.16/12、192.168/16）、回环、链路本地（169.254/16，
    云元数据服务就在这段）、组播、保留与未指定地址；IPv4 映射的 IPv6
    （::ffff:127.0.0.1）先还原再判。非 IP 字符串（是域名）返回 False。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


async def _resolve(host: str, port: int) -> list[str]:
    """解析域名的全部 IPv4/IPv6 地址（线程池里跑阻塞的 getaddrinfo）。

    单独抽出是为了可测：测试里直接 monkeypatch 本函数就能模拟
    「公网域名解析到内网」的绕法，不必真去碰 DNS。
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"域名解析失败：{host}") from exc
    return [str(info[4][0]) for info in infos]


async def assert_safe_target(url: str, *, allow_private: bool | None = None) -> str:
    """抓页前的统一闸门：scheme + 内网主机名 + **DNS 解析后**的 IP 都判一遍。

    比旧版只判 IP 字面量的拦截强一层：「公网域名 A 记录指向 127.0.0.1」
    这类绕法在解析环节就会被拦下。诚实的已知边界：检查与 httpx 真正建连
    之间仍有微小的 DNS rebinding 窗口（TTL 极短的域名理论上可在两次解析间
    换答案）；要彻底封死得自己持解析结果发起连接，这里不为此引私有 transport。
    """
    target = validate_url(url)
    allowed = (
        get_settings().web_fetch_allow_private if allow_private is None else allow_private
    )
    if allowed:
        return target
    parsed = urlparse(target)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("网址缺少域名")
    if host in _DENY_HOSTS or host.endswith(_DENY_SUFFIXES):
        raise ValueError("禁止访问内网地址")
    if ip_denied(host):  # IP 字面量（含 IPv6：hostname 已去方括号）直接判
        raise ValueError("禁止访问内网或保留地址")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    for ip in await _resolve(host, port):
        if ip_denied(ip):
            raise ValueError(f"域名 {host} 解析到内网/保留地址，禁止访问")
    return target


async def fetch_page(
    url: str,
    *,
    max_chars: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    allow_private: bool | None = None,
) -> PageText:
    """下载 ``url`` 并抽取它的可读正文。

    预期内的失败（scheme 不对、内网地址、网络错、非 2xx、内容不是文本、体积超限、
    正文为空）一律抛 ``ValueError`` + 中文描述，由全局处理器翻译成 HTTP 400。

    SSRF 防线收在这里而不是调用方：知识库录入与对话工具共用本函数，只拦一头
    等于没拦。重定向不再交给 httpx 自动跟随，而是一跳一跳手动跟、每一跳都重新
    过 :func:`assert_safe_target`——旧写法里「跟完再复查落点」其实内网请求已经
    发出去了，数据都拿回来了才说不信。
    """
    target = await assert_safe_target(url, allow_private=allow_private)
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,  # 手动跟随：每跳落点都要过闸门
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            current = target
            for _ in range(_MAX_REDIRECTS):
                resp = await client.get(current)
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        break
                    current = await assert_safe_target(
                        urljoin(str(resp.url), location), allow_private=allow_private
                    )
                    continue
                break
            else:
                raise ValueError(f"重定向超过 {_MAX_REDIRECTS} 次，已放弃抓取")
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
