"""联网检索：给对话模型当工具用的搜索源适配层。

三个源按 ``APP_WEB_SEARCH_PROVIDER`` 选，**选定的源缺 key 就自动降级到 bing 抓取**
（而不是直接报错），这样没申请 key 也能立刻体验完整链路，申请到 key 只改一行 .env。

实测本机出网是残缺的（Google / DuckDuckGo / Wikipedia / 公共 SearXNG 全部连不通，
只有 Bing 与三个商用搜索 API 可达），所以别照搬网上教程默认 DDG。

设计约束：

- 只暴露 ``search()`` 一个入口，返回统一的 ``SearchResult``；各家响应结构差异
  被封在本文件的三个适配器里，工具层与模型都不该看到原始 JSON。
- 结果条数与摘要长度都由配置截断：搜索摘要动辄上千字，不截断会让 token 随条数
  线性膨胀，几轮工具循环就能把上下文撑爆。
- 进程内 TTL 缓存：同一句话常被"再答一次"重复触发，而搜索额度是按次计费的。
- 一切失败都抛 ``SearchError``（带中文原因），由工具层转成给模型看的失败文本——
  模型需要知道"查不到"才能改口，静默返回空列表只会让它编造。
"""

from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
SERPER_URL = "https://google.serper.dev/search"
BING_URL = "https://www.bing.com/search"

# Bing 对明显的 bot UA 返回残缺页（实测没有 b_algo 块），必须用浏览器 UA。
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SOURCES = ("tavily", "serper", "bing")

_TAG_RE = re.compile(r"<[^>]+>")
_CACHE: dict[tuple[str, int], tuple[float, list[SearchResult]]] = {}
_CACHE_MAX = 128

# 查询词切分：空白加中英文标点。
_TERM_SPLIT_RE = re.compile(r"[\s,，。、；;：:！!？?（）()【】\[\]\"'“”]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def terms_of(query: str) -> list[str]:
    """查询里的关键词（长度 >=2 才算，单字如「赛」没有区分度）。"""
    out: list[str] = []
    for part in _TERM_SPLIT_RE.split(query or ""):
        token = part.strip().lower()
        if len(token) >= 2:
            out.append(token)
    return list(dict.fromkeys(out))


def _gate_terms(terms: list[str]) -> list[str]:
    """判相关用的词：中文连写额外切 2-gram。

    整串比对会漏掉正确答案——查询「苏州天气」时「苏州-天气预报」并不包含
    连续四字，但显然就是我们要的那条。
    """
    out: list[str] = []
    for term in terms:
        out.append(term)
        if _CJK_RE.fullmatch(term) and len(term) > 2:
            out.extend(term[i : i + 2] for i in range(len(term) - 1))
    return list(dict.fromkeys(out))


def _hay(result: SearchResult) -> str:
    return f"{result.title} {result.snippet} {result.url}".lower()


def _any_match(result: SearchResult, gate: list[str]) -> bool:
    """与查询有没有任何一点相关性（一条都不沾的就是 Bing 的降级垃圾）。"""
    if not gate:
        return True
    hay = _hay(result)
    return any(term in hay for term in gate)


def _hit_count(result: SearchResult, terms: list[str]) -> int:
    """命中的关键词个数，只用来排序：全命中的强相关结果排前面。

    刻意不用「必须全命中」当过滤条件——实测「北京 今天 天气」会把
    「北京天气预报,北京 7 天天气…」这类正确结果误杀。
    """
    hay = _hay(result)
    return sum(1 for term in terms if term in hay)


# 没有检索价值的口语：尤其是模型把用户整句话当搜索词时（实测
# 「赛力斯离开华为之后会怎么样」在 Bing 上会降级成字典页，而
# 「赛力斯离开华为」能出新闻）。
_STOP_TAIL_RE = re.compile(
    r"(说一下|帮我|告诉我|请问|之后|以后|会|的|了|吧|吗|呢|啊|怎么样|怎样|如何|是什么|为什么|有没有)+$"
)


def _trim(term: str) -> str:
    out = term
    while True:
        stripped = _STOP_TAIL_RE.sub("", out).strip()
        if stripped == out:
            return out
        out = stripped


def _refine_query(query: str, terms: list[str]) -> list[str]:
    """零匹配时的候选改写（按顺序试，命中就停）。

    两类失败形态都得接：多词查询（收窄到最有区分度的两个词）与
    单串连写（去掉口语尾巴再截短）——后者恰恰是模型拿用户原句当关键词。
    """
    out: list[str] = []
    if len(terms) >= 2:
        out.append(" ".join(sorted(terms, key=len, reverse=True)[:2]))
    trimmed = _trim(query)
    for cand in (trimmed, _trim(trimmed[:8])):
        cand = cand.strip()
        if len(cand) >= 2 and cand != query and cand not in out:
            out.append(cand)
    return out[:2]


class SearchError(RuntimeError):
    """搜索失败（网络、鉴权、额度、解析），消息已是给终端/模型看的中文。"""


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


def _strip_html(raw: str) -> str:
    return _TAG_RE.sub("", raw or "").replace("&amp;", "&").strip()


def _clean(value: object, limit: int) -> str:
    """压成单行并按上限截断（保留可读的省略号，让模型知道后面还有）。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text


def resolve_source() -> tuple[str, str]:
    """返回（实际使用的源, 降级说明）。降级说明为空表示按配置走。

    需要 key 的源在缺 key 时不该让整个功能不可用，但也不能悄悄换源——把换了什么
    带回去，工具层才能既告诉模型、也让前端明说「本次用的是兜底源」。
    """
    settings = get_settings()
    want = (settings.web_search_provider or "tavily").lower()
    if want not in SOURCES:
        want = "tavily"
    if want == "bing" or settings.web_search_api_key:
        return want, ""
    note = f"{want} 未配置 APP_WEB_SEARCH_API_KEY，已退回 bing 免 key 抓取"
    logger.warning("%s", note)
    return "bing", note


async def search(query: str, count: int | None = None) -> list[SearchResult]:
    """搜索并返回归一化结果（按配置截断条数与摘要长度）。"""
    settings = get_settings()
    query = query.strip()
    if not query:
        raise SearchError("搜索词为空")
    wanted = min(
        max(count or settings.web_search_max_results, 1),
        max(settings.web_search_max_results, 1),
    )
    source, _ = resolve_source()
    key = (source, query.lower(), wanted)
    cached = _CACHE.get(key)
    ttl = settings.web_search_cache_ttl_seconds
    if cached and ttl > 0 and time.time() - cached[0] < ttl:
        return cached[1]

    runner = {"tavily": _search_tavily, "serper": _search_serper, "bing": _search_bing}[source]
    terms = terms_of(query)
    results = await runner(query, wanted)
    kept = [r for r in results if _any_match(r, _gate_terms(terms))]

    if not kept:
        # Bing 对长/口语化的中文查询会静默降级：实测「赛力斯 华为 渠道」报 10 万条
        # 结果，前几条全是「赛」字字典页（换 cn.bing.com、加 mkt 都一样）。改写重试
        # 一次：同一份预算能救回一次真实检索，比直接报“搜不到”对模型更有用。
        for cand in _refine_query(query, terms):
            logger.info("%s 结果与「%s」零匹配，改写重试：%s", source, query, cand)
            retried = await runner(cand, wanted)
            kept = [r for r in retried if _any_match(r, _gate_terms(terms_of(cand)))]
            if kept:
                break

    if not kept:
        # 空结果也要让模型知道，否则它会当成"搜到了但 irrelevant"继续编
        raise SearchError(f"{source} 没有返回与「{query}」相关的结果（换更具体的关键词再试）")
    # 稳定排序：命中关键词多的排前面，模型第一眼看到的就是强相关那条
    kept.sort(key=lambda r: -_hit_count(r, terms))

    if ttl > 0:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        # 缓存键仍是原查询：收窄只是本次检索的内部手段，对调用方透明
        _CACHE[key] = (time.time(), kept)
    return kept


async def _search_tavily(query: str, count: int) -> list[SearchResult]:
    """Tavily 面向 LLM，结果自带抽取好的正文片段，省掉一次抓取往返。"""
    settings = get_settings()
    payload = {
        "query": query,
        "max_results": count,
        "include_answer": False,
        "search_depth": "basic",
    }
    async with httpx.AsyncClient(timeout=settings.web_search_timeout_seconds) as client:
        try:
            resp = await client.post(
                settings.web_search_base_url or TAVILY_URL,
                json=payload,
                headers={"Authorization": f"Bearer {settings.web_search_api_key}"},
            )
        except httpx.HTTPError as exc:
            raise SearchError(f"tavily 请求失败：{exc}") from exc
    if resp.status_code >= 400:
        raise SearchError(f"tavily 返回 HTTP {resp.status_code}：{_clean(resp.text, 160)}")
    data = resp.json() if resp.content else {}
    items = data.get("results") if isinstance(data, dict) else None
    limit = settings.web_search_snippet_chars
    return [
        SearchResult(
            title=_clean(r.get("title"), 200),
            url=str(r.get("url") or "").strip(),
            snippet=_clean(r.get("content"), limit),
        )
        for r in items or []
        if isinstance(r, dict) and str(r.get("url") or "").startswith("http")
    ][:count]


async def _search_serper(query: str, count: int) -> list[SearchResult]:
    """Serper：Google 结果的干净 JSON 封装，只有链接与摘要，要看正文得再抓。"""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.web_search_timeout_seconds) as client:
        try:
            resp = await client.post(
                settings.web_search_base_url or SERPER_URL,
                json={"q": query, "num": count},
                headers={
                    "X-API-KEY": settings.web_search_api_key,
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise SearchError(f"serper 请求失败：{exc}") from exc
    if resp.status_code >= 400:
        raise SearchError(f"serper 返回 HTTP {resp.status_code}：{_clean(resp.text, 160)}")
    data = resp.json() if resp.content else {}
    items = data.get("organic") if isinstance(data, dict) else None
    limit = settings.web_search_snippet_chars
    return [
        SearchResult(
            title=_clean(r.get("title"), 200),
            url=str(r.get("link") or "").strip(),
            snippet=_clean(r.get("snippet"), limit),
        )
        for r in items or []
        if isinstance(r, dict) and str(r.get("link") or "").startswith("http")
    ][:count]


async def _search_bing(query: str, count: int) -> list[SearchResult]:
    """免 key 兜底：直接抓 Bing 结果页。改版即失效，所以只当降级路径。"""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(
            timeout=settings.web_search_timeout_seconds, follow_redirects=True
        ) as client:
            resp = await client.get(
                BING_URL,
                params={"q": query, "count": count},
                headers={"User-Agent": BROWSER_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            )
    except httpx.HTTPError as exc:
        raise SearchError(f"bing 抓取失败：{exc}") from exc
    if resp.status_code >= 400:
        raise SearchError(f"bing 返回 HTTP {resp.status_code}")
    return _parse_bing(resp.text, count)[:count]


def _parse_bing(html: str, count: int) -> list[SearchResult]:
    """从结果页切出（标题 / 链接 / 摘要）。

    刻意用"按 ``<li class="b_algo"`` 切块 + 块内宽松匹配"而不是整段正则：Bing 会在
    ``<h2>`` 与 ``<a>`` 之间插入 ``<div>``/属性，收紧的写法实测一条都命中不了。
    """
    settings = get_settings()
    limit = settings.web_search_snippet_chars
    out: list[SearchResult] = []
    for chunk in html.split('<li class="b_algo"')[1:]:
        chunk = chunk.split("</li>")[0]
        link = re.search(r"<h2[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", chunk, re.S)
        if not link:
            continue
        url = _unwrap_bing_url(link.group(1))
        if not url.startswith("http"):
            continue
        snip = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S)
        snippet = _clean(_strip_html(snip.group(1) if snip else ""), limit)
        out.append(
            SearchResult(
                title=_clean(_strip_html(link.group(2)), 200),
                url=url,
                snippet=snippet,
            )
        )
        if len(out) >= count:
            break
    return out


def _unwrap_bing_url(href: str) -> str:
    """Bing 有时把真实地址包成 ``/ck/a?!!&...&u=a1<base64>``，解出来才是目标站。

    先归一 ``&amp;``：href 来自 HTML 属性，实体写法不归一就拆不出 ``u=a1`` 这一段。
    """
    url = unquote((href or "").replace("&amp;", "&")).strip()
    if "/ck/a" not in url:
        return url
    query = urlparse(url).query
    for pair in query.split("&"):
        if not pair.startswith("u=a1"):
            continue
        token = pair[len("u=a1") :]
        try:
            padded = token + "=" * (-len(token) % 4)
            return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 解不出就退回原样，别让一条脏结果打死整次搜索
            return url
    return url


def reset_cache() -> None:
    """清缓存（改配置或测试用）。"""
    _CACHE.clear()
