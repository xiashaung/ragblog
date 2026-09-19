"""把「联网查询」做成模型可以自己调用的工具。

两个工具：``web_search``（搜索拿链接+摘要）与 ``fetch_page``（觉得某条需要细看时抓全文）。
搜索源适配在 ``web_search`` 服务里，网页解析复用知识库录入那套 ``web_page``。

为什么这一层要单独存在：

- **工具是模型的输出面**，schema 的描述文字会直接决定 4-bit 本地模型会不会真的去调、
  参数填得对不对，所以措辞与示例都写在这里而不是散落到调用点。
- **执行结果要同时喂两个受众**：给模型的是一段编号纯文本（省 token、好引用），
  给前端的是结构化 ``sources``（要渲染成可点链接）。同一份结果两种投影。
- **安全边界在这一层**，不在 ``web_page``：知识库录入的 URL 是人填的，而这里的 URL
  是模型生成的——模型完全可能被网页内容诱导去请求 ``169.254.169.254`` 或内网服务，
  所以私网拦截必须挂在工具执行上，且重定向后的最终地址也要复查。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from ..config import get_settings
from ..schemas.ai import (
    ChatChunk,
    ChatMessage,
    ToolCall,
    ToolEvent,
    ToolFunction,
    Usage,
    WebSource,
)
from . import reasoning, web_page, web_search

logger = logging.getLogger(__name__)

# 主机名形式的内网标识（IP 字面量另走 ipaddress 判断）。
_PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".lan", ".cluster.local")
_PRIVATE_HOSTS = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "nvme1.local",
}

WEB_SEARCH_SPEC: dict = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "搜索公开的网页，用于获取你不知道的最新信息：时效性内容（新闻、天气、"
            "价格、版本号、发布日期）、你不确定的事实，或用户明确要求联网核查时。"
            "不需要查询的常识、闲聊、以及基于给定材料的问题不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，尽量具体，中文或英文"},
                "count": {
                    "type": "integer",
                    "description": "希望返回多少条结果，1-10，默认 5",
                },
            },
            "required": ["query"],
        },
    },
}

FETCH_PAGE_SPEC: dict = {
    "type": "function",
    "function": {
        "name": "fetch_page",
        "description": (
            "抓取一个网页并返回正文纯文本。当 web_search 的摘要不足以回答、"
            "需要看某一页的完整内容（表格、步骤、代码、结论）时，"
            "传入搜索结果里的原始链接（不要自己拼链接）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "必须是之前搜索结果里出现的 http(s) 链接"},
            },
            "required": ["url"],
        },
    },
}

TOOL_SPECS: list[dict] = [WEB_SEARCH_SPEC, FETCH_PAGE_SPEC]

# 工具之外的行为约束：本地小模型不写清楚就会「搜到了也不引用」或「编一个链接」。
WEB_SYSTEM_HINT = (
    "你可以调用 web_search（搜索网页）和 fetch_page（读取指定网页）两个工具获取实时信息。\n"
    "规则：\n"
    "1. 需要最新或你不确定的事实时先调用工具，不要凭记忆编造，也不要给出虚构的链接。\n"
    "2. 回答时把依据的网页以「标题 + 原始链接」列在末尾，链接只能来自工具返回结果。\n"
    "3. 工具返回失败或结果为空时，直接说明查不到，并给出你的判断，不要反复重试同一个词。\n"
    "4. 闲聊、改写、翻译、以及基于用户所给材料的问题不需要联网，直接作答。"
)

# 历史里出现过这类自我否定时，得明确纠一次：小模型会模仿自己上几轮的说法，
# 一句“我无法联网”进了上下文，后面问什么它都懒得调工具（实测会被复读好几轮）。
REFUSAL_CORRECTION = (
    "更正：上面历史消息里“无法联网 / 不能实时查询 / 运行在封闭环境”的说法已经失效。"
    "你现在可以调用 web_search 与 fetch_page，工具也已启用。"
    "请重新调用工具获取实时数据，不要照搬之前的拒绝。"
)


@dataclass(slots=True)
class ToolResult:
    """一次工具执行的结果。

    ``content`` 是回喂给模型的文本（失败时是中文原因，模型据此改口）；
    ``sources`` 是给前端的结构化链接（同一条结果的第二种投影，不喂模型）。
    """

    name: str
    content: str
    ok: bool = True
    sources: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None

    def as_chunk_payload(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "error": self.error,
            "sources": self.sources,
        }


def assert_public_url(url: str, *, allow_private: bool | None = None) -> str:
    """拒绝内网/回环地址，挡住被模型诱导的 SSRF。

    已知边界：这里只判 IP 字面量与常见内网主机名，**不解析 DNS**，所以「公网域名解析到
    10.x」这类绕法拦不住（要拦得自己解析后再连）。重定向落点由调用方复查。
    """
    target = web_page.validate_url(url)  # scheme 白名单 + 域名非空
    allowed = get_settings().web_fetch_allow_private if allow_private is None else allow_private
    if allowed:
        return target
    host = (urlparse(target).hostname or "").lower().rstrip(".")
    if host in _PRIVATE_HOSTS or host.endswith(_PRIVATE_HOST_SUFFIXES):
        raise ValueError("禁止访问内网地址")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return target  # 是域名：交给网络层，按上面的边界说明处理
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
        raise ValueError("禁止访问内网或保留地址")
    if ip.version == 6 and ip.ipv4_mapped is not None:
        mapped = ip.ipv4_mapped
        if mapped.is_private or mapped.is_loopback or mapped.is_link_local:
            raise ValueError("禁止访问内网地址（IPv4 映射）")
    return target


async def execute(name: str, arguments: str | dict | None) -> ToolResult:
    """执行一个工具调用；任何异常都收敛成失败结果，绝不让它击穿整条流。"""
    args = _parse_args(arguments)
    try:
        if name == "web_search":
            return await _run_search(str(args.get("query") or ""), args.get("count"))
        if name == "fetch_page":
            return await _run_fetch(str(args.get("url") or ""))
        return ToolResult(name=name, ok=False, content=f"未知工具：{name}", error="unknown_tool")
    except web_search.SearchError as exc:
        return ToolResult(name=name, ok=False, content=f"搜索失败：{exc}", error=str(exc))
    except (ValueError, httpx.HTTPError) as exc:
        return ToolResult(name=name, ok=False, content=f"执行失败：{exc}", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - 工具挂了不该让回答整个失败
        logger.exception("工具 %s 执行异常", name)
        return ToolResult(
            name=name, ok=False, content=f"执行失败：{type(exc).__name__}", error=str(exc)
        )


def _parse_args(arguments: str | dict | None) -> dict:
    if isinstance(arguments, dict):
        return arguments
    raw = (arguments or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        # 小模型偶尔吐出半截 JSON；把它当 query 用，比直接失败更可能救回这一轮
        match = re.search(r'["\']?(?:query|q|url)["\']?\s*[:=]\s*["\']([^"\']+)', raw)
        fallback = match.group(1) if match else raw[:200]
        key = "url" if raw.lstrip().startswith('{"url') or "http" in raw[:40] else "query"
        return {key: fallback}
    return data if isinstance(data, dict) else {"query": str(data)}


async def _run_search(query: str, count: object) -> ToolResult:
    wanted = None
    if isinstance(count, (int, float)) and int(count) > 0:
        wanted = int(count)
    results = await web_search.search(query, wanted)
    sources = [r.as_dict() for r in results]
    lines = [f"搜索「{query}」得到 {len(results)} 条结果："]
    for i, r in enumerate(results, start=1):
        lines.append(f"{i}. {r.title}\n   链接：{r.url}\n   摘要：{r.snippet or '（无摘要）'}")
    lines.append("请基于以上内容作答，并在结尾列出你实际引用的编号与链接。")
    return ToolResult(
        name="web_search",
        content="\n".join(lines),
        ok=True,
        sources=sources,
    )


async def _run_fetch(url: str) -> ToolResult:
    if not url:
        return ToolResult(name="fetch_page", ok=False, content="缺少 url 参数", error="bad_args")
    try:
        target = assert_public_url(url)
    except ValueError as exc:
        return ToolResult(
            name="fetch_page", ok=False, content=f"该地址不允许访问：{exc}", error="blocked"
        )
    settings = get_settings()
    page = await web_page.fetch_page(
        target,
        max_chars=settings.web_fetch_max_chars,
        timeout=settings.web_fetch_timeout_seconds,
    )
    # 重定向可能被带到内网，落点再查一次（httpx 已在内部跟随）
    if page.final_url != target:
        try:
            assert_public_url(page.final_url)
        except ValueError as exc:
            return ToolResult(
                name="fetch_page",
                ok=False,
                content=f"该地址被重定向到不允许访问的内网地址：{exc}",
                error="blocked",
            )
    title = page.title or target
    body = page.text + ("\n（内容过长，已截断）" if page.truncated else "")
    return ToolResult(
        name="fetch_page",
        content=f"网页《{title}》（{page.final_url}）正文：\n{body}",
        ok=True,
        sources=[{"title": title, "url": page.final_url, "snippet": page.text[:200]}],
    )


# ---------------- 工具循环：“生成 → 执行工具 → 再生成” ----------------

ROUND_LIMIT_HINT = (
    "已达到工具调用次数上限，工具已停用。不要再输出任何工具调用格式，"
    "只看上面已有的搜索结果，现在直接给出结论：先说结论，再在末尾列出你引用的标题与链接；"
    "没查到的部分明确说“没查到”。"
)

ROUND_LIMIT_EMPTY = (
    "检索次数用完了，但没能整理出结论，这次也没拿到可引用的网页。"
    "可以再问一次（或点「再答一次」），也可以把问题拆小一点：只问一个对象、限定时间。"
)

ROUND_LIMIT_CALLED = (
    "检索次数用完了，模型仍在要求继续搜索（这些请求没被执行），所以这里没有可靠结论。"
    "把问题拆小一点（只问一个对象、限定时间）再问一次通常就能拿到答案。"
)

# 实测过的事实：模型会“苏州实时天气”→“苏州今天天气 实时”→“苏州今日天气 温度”
# 这样连搜三轮，每次拿回同一批结果，把轮数烧光也没新信息，最后什么都答不上。
# 结果集合重复本身就是“再搜也不会有新东西”的确定信号，比靠提示词求它收手更可靠。
REPEAT_STOP_HINT = (
    "已停止继续搜索：刚才换了词但结果与前面完全相同，不会又有新信息。"
    "请基于上面已有的搜索结果直接给出结论，确实查不到的部分就说明查不到。"
)

# 模型把函数调用当正文吐出来时的标记（Qwen / Hermes 风格）。三种情形必现：
# 一是撞了轮数上限、工具被撤掉，它仍想调；二是端点没做原生解析（mlx-lm 类）；
# 三是上一轮工具结果太长把它冲掉了。不处理前端就是一屏 XML（已实测）。
_CALL_NAME = r"(?:tool|function)[_-]?calls?"
# 包裹层（tool_call 尖括号形式与 Hermes 的管道形式）的开与闭：管道形式也得算开标记，
# 否则模型用 <|tool_call|> 时那段会被当正文漏出去
_WRAP_OPEN_RE = re.compile(rf"<\|?\s*{_CALL_NAME}\s*\|?>", re.I)
_WRAP_CLOSE_RE = re.compile(rf"<\|?\s*/\s*{_CALL_NAME}\s*\|?>", re.I)
_FUNC_RE = re.compile(r"<\s*function\s*=\s*([A-Za-z0-9_.\-]+)\s*>", re.I)
_PARAM_RE = re.compile(r"<\s*parameter\s*=\s*([^\s<>]+)\s*>", re.I)
_PARAM_CLOSE_RE = re.compile(r"<\s*/\s*parameter\s*>", re.I)
_ANY_TAG_RE = re.compile(r"<[^<>]*>")
# 裸块（没有包裹层）的兼容退出：/function、/tool_calls 这类变体也算，
# 认不出结尾就退出块状态的话，后面的正文会被当成标记整段吞掉
_CLOSE_SCAN_RE = re.compile(r"<\|?\s*/\s*(?:tool|function)[^<>]*>", re.I)
# 标记的候选前缀：结尾正好落在这些上面时得攒住等下一块（判断半个标记用）。
# 闭合形式也要收：被 max_tokens 截断时只剩 ``</tool_ca`` 这种尾巴，不攒住就会当正文漏出
_MARKER_CANDIDATES = (
    "<tool_call>",
    "<function_call>",
    "<tool-call>",
    "<function-call>",
    "<function=web_search>",
    "<function=fetch_page>",
    "<|tool_call|>",
    "<|function_call|>",
    "</tool_call>",
    "</function_call>",
    "</function>",
)
_MARKER_PREFIXES = tuple(
    dict.fromkeys(p[: i + 1] for p in _MARKER_CANDIDATES for i in range(len(p)))
)
_KNOWN_TOOLS = {"web_search", "fetch_page"}
# 小模型爱自己编工具名：能对上意图的就映射，比丢掉这一轮好
_NAME_ALIASES = {
    "search": "web_search",
    "websearch": "web_search",
    "websearchtool": "web_search",
    "fetch": "fetch_page",
    "fetchurl": "fetch_page",
    "readurl": "fetch_page",
    "readpage": "fetch_page",
}

_OUT, _IN_BLOCK, _IN_BARE = 0, 1, 2


def _earliest(text: str, *patterns: re.Pattern[str]) -> re.Match[str] | None:
    """几个模式里位置最靠前的那个匹配（定块的边界用）。"""
    best: re.Match[str] | None = None
    for pattern in patterns:
        match = pattern.search(text)
        if match and (best is None or match.start() < best.start()):
            best = match
    return best


def _hold_back(buf: str) -> int:
    """结尾有多少字符可能是半个开标记，得先攒住等下一块。"""
    low = buf.lower()
    for i in range(len(low) - 1, max(len(low) - 48, -1), -1):
        if low[i] != "<":
            continue
        tail = low[i:]
        if any(candidate.startswith(tail) for candidate in _MARKER_PREFIXES):
            return len(low) - i
    return 0


def _is_marker_tail(buf: str) -> bool:
    """缓冲末尾这段是不是“没写完的标记”（流被 max_tokens 切在标记中间时会出现）。

    不能只判 ``buf`` 是某个候选的前缀：截断后 buffer 是 ``\n<tool_call>`` 这种
    带前导换行的形态，得从最后一个 ``<`` 起判。长度卡到 2 以上是为了避免
    把“a<”这种比大小的正文尾巴吞掉。
    """
    low = buf.lower()
    head = max(low.rfind("<"), low.rfind("|"))
    tail = low[head:]
    if len(tail) < 2:
        return False
    return any(candidate.startswith(tail) for candidate in _MARKER_CANDIDATES)


class CallMarkupScrubber:
    """从流式正文里剥出「被当文本吐出的函数调用」，能还原成 ToolCall 就还原。

    为什么不是逐块跑正则：内容是一小段一小段到的，标记会被切在任意两块之间
    （实测 ``<fun`` + ``ction=web_search>``），逐块匹配必然漏掉跨界的那一半。

    为什么不直接删：模型写调用说明它想查，删掉就只能拿旧结果编一个结论；
    还原成真的工具回合去执行，这一轮才真的能给出带来源的答案。

    用法：每块喂 :meth:`feed`（返回可以安全外发的文本），流结束后调 :meth:`close`
    收尾，再读 :attr:`calls` 拿还原出来的工具调用。
    """

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []
        self._buf = ""
        self._state = _OUT
        self._name = ""
        self._seq = 0

    def feed(self, text: str) -> str:
        self._buf += text
        return self._drain()

    def close(self) -> str:
        """流结束：结算缓冲（未闭合的调用按已生成部分收下）。"""
        if self._state != _OUT:
            self._finish(self._name, self._buf)
            self._buf = ""
            self._state = _OUT
            self._name = ""
            return ""
        out, self._buf = self._buf, ""
        if _is_marker_tail(out):
            # 流正好切在标记中间：能认出来的调用照样收下，那段半截标记不能当正文外发
            rest = out[: max(out.rfind("<"), out.rfind("|"))]
            self._finish("", out[len(rest) :])
            return rest
        return out

    # ---- 内部 ----

    def _drain(self) -> str:
        out: list[str] = []
        while True:
            if self._state == _OUT:
                match = _earliest(self._buf, _WRAP_OPEN_RE, _FUNC_RE, _CLOSE_SCAN_RE)
                if match is None:
                    safe = len(self._buf) - _hold_back(self._buf)
                    out.append(self._buf[:safe])
                    self._buf = self._buf[safe:]
                    break
                out.append(self._buf[: match.start()])
                tail = self._buf[match.start() :]
                if not _WRAP_OPEN_RE.match(tail) and not _FUNC_RE.match(tail):
                    # 落单的闭合标记：整块被截断时只剩个尾巴，留着就是一行脏 XML
                    self._buf = tail[match.end() :]
                    continue
                if not self._begin(tail):
                    break  # 标记还不完整（如 ``<|tool_call|`` 缺尾 ``>``）：等下一块
                continue
            if self._state == _IN_BLOCK:
                if not self._name:
                    # 包裹层与内层函数标记常被切在两块到达（实测第二块才是
                    # ``\n`` + ``function=...>``）：块首那个函数标记是本块的名字，
                    # 不是边界。当成边界处理会把整块丢空，并把参数标记当正文漏出。
                    head = self._buf.lstrip()
                    named = _FUNC_RE.match(head)
                    if named:
                        self._name = named.group(1)
                        self._buf = head[named.end() :]
                        continue
                # 块里的 /function 只是内层闭合（开标记已在 _begin 吃掉），边界只有
                # 包裹层的结尾或下一个新块；用 _CLOSE_SCAN_RE 会在内层闭合处断块
                edge = _earliest(self._buf, _WRAP_CLOSE_RE, _WRAP_OPEN_RE, _FUNC_RE)
            else:
                # 裸 function 块没有包裹：下一个开标记、或落单的闭合标记，就是它的边界
                edge = _earliest(self._buf, _CLOSE_SCAN_RE, _WRAP_OPEN_RE, _FUNC_RE)
            if edge is None:
                break  # 块还没完：继续等下一块，这段一个字符都不能外发
            self._finish(self._name, self._buf[: edge.start()])
            self._buf = self._buf[edge.end() :]
            self._state = _OUT
            self._name = ""
        return "".join(out)

    def _begin(self, tail: str) -> bool:
        """从 ``tail``（以开标记开头）进入块状态并记下函数名。

        返回 False 表示“看得到个开头但还不完整”，调用方得攒着等下一块——
        不返回这个信号的话，扫描会原地反复匹配同一个开头，循环出不来。
        """
        for pattern in (_WRAP_OPEN_RE, _FUNC_RE):
            match = pattern.match(tail)
            if not match:
                continue
            self._buf = tail[match.end() :]
            if pattern is _FUNC_RE:
                self._state = _IN_BARE
                self._name = match.group(1)
                return True
            self._state = _IN_BLOCK
            self._name = ""
            # 紧跟的 <function=name> 属于本块：现在就吃掉，否则块内扫描会把它当成
            # 「又开了一个新块」，而 </function> 又会被误当成整块的结尾
            stripped = self._buf.lstrip()
            inner = _FUNC_RE.match(stripped)
            if inner:
                self._name = inner.group(1)
                self._buf = stripped[inner.end() :]
            return True
        self._buf = tail
        return False

    def _finish(self, name: str, body: str) -> None:
        # 包裹块里的函数名是块内 ``<function=name>`` 给的，进块时拆不出来就在这儿取
        if not name:
            match = _FUNC_RE.search(body)
            if match:
                name = match.group(1)
                body = body[match.end() :]
        args: dict[str, object] = {}
        for match in _PARAM_RE.finditer(body):
            rest = body[match.end() :]
            closed = _PARAM_CLOSE_RE.search(rest)
            value = (rest if closed is None else rest[: closed.start()]).strip()
            args[match.group(1)] = int(value) if value.isdigit() else value
        if not args:
            args = _bare_args(body, name)
        tool = _resolve_name(name)
        if not tool:
            return
        self._seq += 1
        self.calls.append(
            ToolCall(
                id=f"call_text_{self._seq}",
                function=ToolFunction(
                    name=tool, arguments=json.dumps(args, ensure_ascii=False)
                ),
            )
        )


def _resolve_name(name: str) -> str:
    """模型给的工具名归一到已注册的那几个；认不出就返回空（吞掉这个调用）。"""
    key = (name or "").strip().lower().replace("-", "_")
    if key in _KNOWN_TOOLS:
        return key
    return _NAME_ALIASES.get(key.replace("_", ""), "")


def _bare_args(body: str, name: str) -> dict[str, object]:
    """没有 ``<parameter=k>`` 包裹时，整段裸文本就是那一个参数。"""
    plain = _ANY_TAG_RE.sub(" ", body).strip().strip("|>").strip()
    if not plain:
        return {}
    if _resolve_name(name) == "fetch_page":
        urls = re.findall(r"https?://[^\s\"'<>]+", plain)
        return {"url": urls[0] if urls else plain}
    return {"query": plain}


def strip_tool_markup(text: str) -> str:
    """一次性剥掉调用标记（非流式路径用；流式请走 ``CallMarkupScrubber``）。"""
    if "<" not in text:
        return text
    scrubber = CallMarkupScrubber()
    kept = scrubber.feed(text) + scrubber.close()
    return re.sub(r"\n{3,}", "\n\n", kept).strip("\n")


def tools_enabled(web_mode: str) -> bool:
    """总开关在后端：关掉后无论前端传什么都当 off（前端开关只是意图，不是权限）。"""
    return get_settings().web_search_enabled and web_mode != "off"


# 该查却不查时，auto 完全把判断交给一个 4-bit 模型（实测：同样的问题单独问会搜，
# 前面聊过几句“我不能联网”后就改口拒绝）。这段字面识别把“明显需要实时数据”的
# 那类问题在第一轮就硬性先搜一次，让结果可预测。
_TIME_SENSITIVE_RE = re.compile(
    r"天气|气温|温度|下雨|下雪|台风|空气指数|空气质量"
    r"|股价|行情|大盘|指数|汇率|油价|金价|票价"
    r"|比分|赛果|赛程|成绩|排名|榜单|得分"
    r"|新闻|公告|动态|进展|发布|上线|发售"
    # 时事类提法（“…会怎么样”“有什么影响”“近况”）：光靠天气/价格这类名词盖不全公司、人物类问题。
    # 这里宁可误搜不可漏搜：漏了就是拿训练记忆编“现状”，误搜只是多花一次额度。
    # “怎么/什么”都得收：实跑里用户写的是“会有怎么样的影响”，模型复述时写成“什么样”。
    # “影响”不能单独立词（“这个索引对查询性能的影响”是开发问题），只收完整的时事提法
    r"|会[有]?[怎什][么]?[样]?|什么影响|哪些影响|有何影响|怎样的影响|近况|动向|现状|前景如何"
    r"|最新|最近|现在|此刻|今天|今晚|明天|后天|本周|本月|今年"
    r"|多少度|几点|版本号?|最新一?版"
    r"|目前|当前|实时|今日|昨天|前一天"
)

# 反向掩码：纯寒暄/翻译/解释类不要因为带了“今天/现在”就白烧一次搜索额度。
_SMALL_TALK_RE = re.compile(
    r"^(你好|您好|嗨|哈喽|hello|hi|hey|早上好|晚上好|谢谢|多谢|感谢|再见|拜拜|好的|ok)"
    r"|(翻译|改写|润色|校对|错别字|解释一下|什么意思|总结一下|起名|写一段|写一条)"
)


def _last_user_text(messages: list[ChatMessage]) -> str:
    """取最后一条用户消息的纯文本（多模态的 content parts 只拿 text 段）。"""
    for msg in reversed(messages):
        if msg.role != "user":
            continue
        parts = msg.content
        if isinstance(parts, str):
            return parts
        if isinstance(parts, list):
            return " ".join(
                str(p.get("text") or "")
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return ""
    return ""


def needs_web(query: str) -> bool:
    """这个问题明摆着需要实时数据吗（启发式，不做语义理解）。"""
    text = (query or "").strip()
    if not text:
        return False
    if _SMALL_TALK_RE.search(text):
        return False
    return bool(_TIME_SENSITIVE_RE.search(text))


def first_round_choice(web_mode: str, query: str) -> str:
    """首轮该不该先搜（``required`` 会被当成“由服务端先检索一次”的意图）。

    注意：这个值只是**意图**。实测 omlx 一类端点会直接忽略 ``tool_choice=required``
    （required / auto / 不传三种写法触发率一样），所以真需要搜的时候不能指望端点，
    得靠下面的预检索自己执行。后续轮永远是 auto（否则模型拿不到搜索结果会一直要工具）。
    """
    if web_mode == "forced":
        return "required"
    return "required" if needs_web(query) else "auto"


# 历史里助手自己说过“不能联网”就算污染。刻意拆成两个词表判同现，而不是一条贴着写的正则：
# 实跑数据里的措词带markdown 星号、也有插入语（“无法**自动**通过百度…查询”、“不能直接实时联网”），
# 贴着匹配会全部漏掉。
_REFUSAL_DENY_RE = re.compile(r"无法|不能|不可以|没法|没有|不具备|不支持|未能")
_REFUSAL_WEB_RE = re.compile(r"联网|上网|访问网络|互联网|搜索引擎|在线查询")


def _history_denies_web(messages: list[ChatMessage]) -> bool:
    """历史里有没有“我不能联网”式的自我否定（只看助手消息，用户怎么问不算）。"""
    for msg in reversed(messages):
        if msg.role != "assistant" or not isinstance(msg.content, str):
            continue  # 工具回合的助手消息 content 是 None，直接跳过
        if (
            _REFUSAL_DENY_RE.search(msg.content)
            and _REFUSAL_WEB_RE.search(msg.content)
        ):
            return True
    return False


async def stream_with_tools(
    backend: object,
    *,
    messages: list[ChatMessage],
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    web_mode: str = "auto",
) -> AsyncIterator[ChatChunk]:
    """带工具的多轮流式对话，逐块 yield，直到模型给出不带工具调用的回答。

    几个不直觉但重要的细节：

    - **每轮的收尾 chunk 先攒住不发**：它带 ``done=True``，中途发出去前端会当成“整段结束”。
      只有确认本轮不再要工具时才连同累计 usage 一起发出。
    - **``tool_calls`` 是给上层的信号，不透给浏览器**；给浏览器的是 ``tool`` 事件（两 phase）。
    - **思考过程与答案分字段走**：``chunk.reasoning`` 逐块直发（字段路与标签路
      在此汇流），不回喂模型也不进工具回合：那是模型给自己看的过程，
      塞回上下文只会把 prompt 顶胀。上限轮例外：那里的“思考”就是正文本身。
    - **json 模式与工具不共存**：既要 ``response_format=json_object`` 又要函数调用，
      多数兼容端点会直接拒绝，而且结构化输出中间插工具回合会把答案弄碎。
      所以“要 JSON”优先，此时不挂工具（``forced`` 也一样），而不是默默丢掉 JSON 模式。
    - **token 用量是跨轮累加的**：开了联网后一轮提问可能打了三次模型，只报最后一段
      会让前端显示的用量严偏小。
    - **超轮数就剥掉工具强制作答**：不这么做，模型会卡在“再搜一下试试”的循环里烧额度。
    - **``auto`` 不等于完全交给模型**：时效性问题（天气/股价/新闻…）首轮强制先搜，
      历史里出现过“我不能联网”式的自我否定时再补一条更正提示——两者都是为了让
      “该查的问题”必定去查，而不是依赖 4-bit 模型自己想起来它有工具。
    """
    settings = get_settings()
    # 结构化输出优先：它一旦成立，中途开工具会把答案切碎、也会被严格 json 模式拒绝
    enabled = tools_enabled(web_mode) and response_format is None
    msgs: list[ChatMessage] = list(messages)
    if enabled and not any(m.role == "system" for m in msgs):
        # 已有 system 则尊重调用方（知识库问答等场景已自带提示词）
        msgs.insert(0, ChatMessage(role="system", content=WEB_SYSTEM_HINT))
    # 首轮就要用的策略：必须在 append 任何新消息前从原始历史里取最后一条提问
    last_q = _last_user_text(msgs)
    first_choice = first_round_choice(web_mode, last_q)
    if enabled and _history_denies_web(msgs):
        # 放在提问之后、生成之前：越靠近当前轮的指令，小模型越听
        msgs.append(ChatMessage(role="system", content=REFUSAL_CORRECTION))
        logger.info("历史中存在联网自我否定，已注入更正提示并保留工具")

    tools = TOOL_SPECS if enabled else None
    max_rounds = max(settings.web_search_max_rounds, 1) if enabled else 0
    total = Usage()
    round_no = 0
    # 整条流里到底出现过思考内容没有：上限轮靠它决定要不要把思考抹掉。
    # 思考逐块直发也不在这层累计（累计一份再随收尾块发一遍，非流式聚合就会数两份）
    thinking_seen = False
    model_calls = 0  # 预检索不占模型调用，而 ``first_choice`` 只对首次模型调用有意义
    # 累计已经拿到手的真实来源：上限轮模型只吐伪标记时，这些链接就是能给用户的最后答案
    collected: list[WebSource] = []
    seen_urls: set[str] = set()
    # 已经出现过的搜索结果集合（按 URL 集签名）：用来识别“换个词再搜拿到同一批结果”
    search_sigs: set[tuple[str, ...]] = set()

    if enabled and first_choice == "required" and last_q:
        # 预检索：把“必查”寄托在端点遵守 tool_choice 上是假的（实测会被忽略，而且
        # 模型也可能干脆凭记忆开说）。所以判定该搜时我们自己搜一次，再合成一个工具
        # 回合喂进去，模型只负责基于结果作答。
        round_no = 1
        # 预检索不能占模型的调用额度：实跑「预检索 + 一次失败搜索 + 一次成功搜索」
        # 正好把 max_rounds=3 用光，上限轮只吐伪标记，手里握着 5 条好来源却给用户
        # 一句“没额度了”。多给一轮只多一次搜索，但能正常拿到答案。
        max_rounds += 1
        arguments = json.dumps({"query": last_q}, ensure_ascii=False)
        detail = f"预检索：{last_q}"
        call = ToolCall(
            id="call_pre_1",
            function=ToolFunction(name="web_search", arguments=arguments),
        )
        yield ChatChunk(
            tool=ToolEvent(
                phase="call", name="web_search", round=1, arguments=arguments, detail=detail
            )
        )
        result = await execute("web_search", arguments)
        msgs.append(ChatMessage(role="assistant", content=None, tool_calls=[call]))
        msgs.append(
            ChatMessage(
                role="tool",
                tool_call_id=call.id,
                name="web_search",
                content=result.content,
            )
        )
        pre_sources = _to_sources(result.sources)
        _collect_sources(collected, seen_urls, pre_sources)
        if result.ok and result.sources:
            # 预检索那批也要登记签名：模型接着搜同一个话题时拿到的就是同一批结果
            search_sigs.add(_sig_of(result.sources))
        yield ChatChunk(
            tool=ToolEvent(
                phase="result",
                name="web_search",
                round=1,
                ok=result.ok,
                error=result.error,
                detail=detail,
                sources=pre_sources,
            )
        )

    while True:
        round_no += 1
        model_calls += 1
        exhausted = enabled and round_no > max_rounds
        if exhausted:
            # 最后一轮不再声明工具，并明确要求它基于已有信息作答
            tools = None
            msgs.append(ChatMessage(role="system", content=ROUND_LIMIT_HINT))
        closing: ChatChunk | None = None
        calls: list[ToolCall] = []
        scrubber = CallMarkupScrubber()
        # 每一轮模型调用都是新的一段输出，思考标签不会贯穿两轮，所以分段器按轮创建
        splitter = reasoning.ThinkSplitter()
        # 攒住的正文：上限轮整段可能都是伪标记，逐块发会先闪一屏 XML；
        # 收尾块里带的正文也放这里，免得和已经逐块发出的内容重复
        held: list[str] = []
        repeated = False  # 本轮里有没有“搜到了重复结果”：有则下一轮就停止搜索
        async for chunk in backend.chat_stream(  # type: ignore[attr-defined]
            msgs,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            tools=tools,
            # 首轮带上“该搜”的意图（守规矩的端点会真的强制调工具）；走了预检索时
            # 它依然是第一次模型调用，不能因为 round_no 被预检索占位就失效
            tool_choice=first_choice if model_calls == 1 else "auto",
        ):
            if chunk.usage:
                # 先记账再看 tool_calls：收尾块往往同时带着 usage 与工具调用，
                # 先 continue 会把工具回合的 token 漏掉，前端显示的用量会严偏小
                total = _add_usage(total, chunk.usage)
            if chunk.reasoning:
                # 字段路（reasoning_content）：已是干净的思考增量，不进 scrubber。
                # 收尾块携带的整段思考不单独发（那份已在 ``closing`` 里，再发就是两份）
                thinking_seen = True
                if not chunk.done:
                    yield ChatChunk(reasoning=chunk.reasoning)
            if chunk.content:
                # 标签路：先拆成「思考 / 正文」两类片段，正文再交给剔工具标记的 scrubber
                for kind, piece in splitter.feed(chunk.content):
                    if kind == reasoning.THINK:
                        thinking_seen = True
                        # 上限轮不逐块透思考：整段输出都要先攒住，收尾时再定性
                        if not exhausted:
                            yield ChatChunk(reasoning=piece)
                        continue
                    out = scrubber.feed(piece)
                    if exhausted or chunk.done:
                        held.append(out)
                    elif out:
                        yield ChatChunk(content=out)
            if chunk.tool_calls:
                calls = chunk.tool_calls
            if chunk.done:
                # 正文已经单独处理过了，收尾块不能再带一份。用空串而不是 None：
                # model_copy 不走校验，填 None 会让 NDJSON 出现 ``"content": null``，
                # 前端与测试拼接文本时都会被当成另一个东西。
                # ``reasoning`` 不清：非流式网关会把整段思考放在收尾块里
                closing = chunk.model_copy(update={"content": ""})
                continue
            if not chunk.content and not chunk.reasoning:
                yield chunk

        held.append(scrubber.close())
        # 收尾结算分段器：留在缓冲里的尾巴要在这里定性，不能丢
        for kind, piece in splitter.close():
            if kind != reasoning.THINK:
                if piece:
                    held.append(piece)
                continue
            thinking_seen = True
            if not exhausted:
                # 普通轮：缓冲尾巴（未闭合思考块等）直接补一笔给前端。
                # 上限轮不补：本轮思考与正文其实是同一段东西，再发一份只会重复
                yield ChatChunk(reasoning=piece)
        text = "".join(held)
        if exhausted:
            # 超轮数后即使端点仍塞回 tool_calls 也不再执行：不兜住这一步，
            # 一个不听话的后端就能把循环顶成死循环、把搜索额度烧光
            yield _merge_usage(
                _finalize_exhausted(
                    closing, text, scrubber.calls, collected, suppress_thinking=thinking_seen
                ),
                total,
                thinking_seen,
            )
            return
        if text:
            yield ChatChunk(content=text)
        if not calls:
            # 端点没把调用解析成结构化字段：模型把调用写进了正文，还原成真的工具
            # 回合继续问（上限轮已在上面返回，不会因此死循环）
            calls = scrubber.calls
            if not calls:
                yield _merge_usage(closing, total, thinking_seen)
                return

        # 模型要调工具：先把这个带 tool_calls 的助手回合原样回位，下一轮才能接上
        msgs.append(ChatMessage(role="assistant", content=None, tool_calls=calls))
        for index, call in enumerate(calls):
            name = call.function.name
            arguments = call.function.arguments
            yield ChatChunk(
                tool=ToolEvent(
                    phase="call",
                    name=name,
                    round=round_no,
                    arguments=arguments,
                    detail=_describe(name, arguments),
                )
            )
            result = await execute(name, arguments)
            content = result.content
            if name == "web_search" and result.ok and result.sources:
                sig = _sig_of(result.sources)
                if sig in search_sigs:
                    repeated = True
                    content += "\n" + REPEAT_STOP_HINT
                else:
                    search_sigs.add(sig)
            msgs.append(
                ChatMessage(
                    role="tool",
                    tool_call_id=call.id or f"call_{round_no}_{index}",
                    name=name,
                    content=content,
                )
            )
            round_sources = _to_sources(result.sources)
            _collect_sources(collected, seen_urls, round_sources)
            yield ChatChunk(
                tool=ToolEvent(
                    phase="result",
                    name=name,
                    round=round_no,
                    ok=result.ok,
                    error=result.error,
                    detail=_describe(name, arguments),
                    sources=round_sources,
                )
            )

        if repeated:
            # 下一轮就是“上限轮”：剥掉工具并要求基于已有资料作答。
            # 不等它自己收手：实测它会一直换词搜到额度用尽，反而拿不到答案。
            # 这里只把 REPEAT_STOP_HINT 贴在搜索结果后面（离结果越近越管用），
            # 不再额外补一条 system：连续两条 system 会把下一轮的 ROUND_LIMIT_HINT 挤成噪音
            tools = None
            max_rounds = round_no
            logger.info("检索结果重复，已停止后续搜索并强制作答")


def _sig_of(sources: list[dict]) -> tuple[str, ...]:
    """一批搜索结果的签名（只看 URL 集）：换词重搜时它最能说明“没有新东西”。"""
    return tuple(sorted(str(s.get("url") or "") for s in sources))


def _describe(name: str, arguments: str | None) -> str:
    """把工具参数翻成一句人话（解析不了就退成原始串，不抛错）。"""
    args = _parse_args(arguments)
    if name == "web_search":
        query = str(args.get("query") or "").strip()
        return f"搜索：{query}" if query else "搜索（没拿到关键词）"
    if name == "fetch_page":
        url = str(args.get("url") or "").strip()
        return f"读取网页：{url}" if url else "读取网页（没拿到地址）"
    return name


def _to_sources(items: list[dict]) -> list[WebSource]:
    out: list[WebSource] = []
    for item in items:
        try:
            out.append(WebSource.model_validate(item))
        except Exception:  # noqa: BLE001 - 一条脏链接不该抽掉整个工具事件
            continue
    return out


def _add_usage(base: Usage, extra: Usage) -> Usage:
    return Usage(
        prompt_tokens=base.prompt_tokens + extra.prompt_tokens,
        completion_tokens=base.completion_tokens + extra.completion_tokens,
        total_tokens=base.total_tokens + extra.total_tokens,
    )


def _finalize_exhausted(
    closing: ChatChunk | None,
    text: str,
    calls: list[ToolCall],
    sources: list[WebSource],
    *,
    suppress_thinking: bool = False,
) -> ChatChunk:
    """上限轮的收尾：标记已被 scrubber 剥过，全剥光时给一句人话而不是空气泡。

    ``suppress_thinking`` 只在真的出现过思考时传真：上限轮里被攒住的“思考”
    其实就是模型想说的话（它已经不再调工具，没什么可规划的），再挂个思考区块
    用户会看到同一句说两遍。工具回合里逐块流出过的思考不在本块里，删不掉也不删。
    """
    body = text.strip("\n") or _fallback_answer(calls, sources)
    update: dict[str, object] = {"content": body}
    if suppress_thinking:
        update["reasoning"] = ""
    if closing is None:
        return ChatChunk(done=True, **update)  # type: ignore[arg-type]
    return closing.model_copy(update=update)


def _merge_usage(
    closing: ChatChunk | None,
    total: Usage,
    think_seen: bool = False,
) -> ChatChunk:
    """收尾块只有一份、但用量是几轮累加的，所以盖成累计值。

    收尾块自己带 ``reasoning`` 时（非流式网关把整段思考塞在这）保留它；
    正文里夹着成对思考标签时也拆出来，否则标签会连着答案一起当正文吐给用户。
    上限轮已由 :func:`_finalize_exhausted` 把正文换成兜底句、思考清成空串，
    这里看到 ``reasoning == ""`` 就不再从正文里拆（那份“思考”其实就是正文本身）。
    """
    if closing is None:
        return ChatChunk(done=True, usage=total)
    update: dict[str, object] = {"usage": total}
    thinking = closing.reasoning or ""
    if closing.content and (thinking or not think_seen):
        # 先剥工具标记再拆思考标签：两者都是对同一段文本的清理，
        # 顺序反了会把标签体内的伪标记当思考留下
        tagged, body = reasoning.split_completed(strip_tool_markup(closing.content))
        update["content"] = body
        thinking = thinking or tagged
    update["reasoning"] = thinking
    return closing.model_copy(update=update)


def _fallback_answer(calls: list[ToolCall], sources: list[WebSource]) -> str:
    """上限轮什么正文都没留下时的兜底。

    刻意不把“轮数用完”当成唯一答复：手里往往已经有几条真实来源，用户要的是能点
    开的资料，而不是一句看不懂的额度说明（实跑就是这么把答句丢掉了）。
    只列链接不下结论：没模型参与的文字不能替它编一个“现状”。
    """
    if not sources:
        return ROUND_LIMIT_CALLED if calls else ROUND_LIMIT_EMPTY
    lines = [
        f"已检索到 {len(sources)} 条相关资料，但检索次数在模型整理出结论前用完了。"
        "先把原文列在这里，点开即可自行核对；把问题拆小一点（只问一个对象、限定时间）再问一次通常就有结论。"
    ]
    for index, src in enumerate(sources, start=1):
        lines.append(f"{index}. {src.title or src.url}\n   {src.url}")
    return "\n".join(lines)


def _collect_sources(
    bucket: list[WebSource], seen: set[str], items: list[WebSource]
) -> None:
    """按 URL 去重累计来源：多轮搜索常常命中同一条，兜底答案里重复列没法看。"""
    for src in items:
        if src.url in seen:
            continue
        seen.add(src.url)
        bucket.append(src)
