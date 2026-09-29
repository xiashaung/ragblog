"""录入内容的格式归一化：把 HTML 富文本压成 Markdown 源码。

知识库只存一种格式（Markdown 源码），检索与分块看到的都是干净文本，
不会被 ``<span style=...>`` 这类展示标签污染。富文本（Word / 网页编辑器
粘贴）在入口处经本模块转成等价的 Markdown，之后就不再回头解释它了。

和 :mod:`app.services.web_page` 一样，刻意只用标准库 ``html.parser``：
为「粘一段富文本」拖一个 HTML 解析包进来不划算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

#: 这些标签连同子树一起丢掉：脚本/样式/表单/媒体都不算正文
_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "head",
    "meta",
    "link",
    "title",
    "svg",
    "canvas",
    "iframe",
    "object",
    "embed",
    "video",
    "audio",
    "source",
    "track",
    "form",
    "input",
    "button",
    "select",
    "option",
    "textarea",
    "dialog",
    "menu",
}

#: 没有闭合标签的空元素
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}

#: 标题标签 -> 井号个数
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

#: 成块的容器：进出各断一个段落（表格里被单独处理的 td/th 不在其中）
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "center",
    "dd",
    "details",
    "dialog",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "header",
    "main",
    "nav",
    "p",
    "section",
    "summary",
}

#: 行内语义 -> Markdown 包裹符；无对应语义的（u/small/span…）只保留文字
_INLINE_MARKS: dict[str, tuple[str, str]] = {
    "strong": ("**", "**"),
    "b": ("**", "**"),
    "em": ("*", "*"),
    "i": ("*", "*"),
    "cite": ("*", "*"),
    "var": ("*", "*"),
    "del": ("~~", "~~"),
    "s": ("~~", "~~"),
    "strike": ("~~", "~~"),
}

#: 折叠空白时一并把 ``&nbsp;``（转实体后是 U+00A0）换成普通空格，
#: 否则它会以不可见字符的形式留在向量里
_WHITESPACE = re.compile(r"[ \t\r\f\v\u3000\xa0]+")
_BLANK_RUNS = re.compile(r"\n{3,}")


@dataclass
class _Node:
    """极简 DOM 节点：``tag`` 为 ``#text`` 时表示文本节点。"""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    text: str = ""
    children: list[_Node] = field(default_factory=list)

    def get(self, name: str) -> str:
        """取属性（拿不到返空串，调用方都是「有就用、没有就跳过」）。"""
        return (self.attrs.get(name) or "").strip()


class _TreeBuilder(HTMLParser):
    """把 HTML 拼成一棵宽容的树：标签不闭合、错序闭合都不会抛异常。"""

    def __init__(self) -> None:
        """根节点用 ``#root`` 占位，栈顶永远是当前正在填充的元素。"""
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root")
        self._stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """入栈；空元素只落节点不进栈，避免后面的内容被「关」在里面。"""
        parent = self._stack[-1]
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs})
        parent.children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """自闭合写法（``<br/>``）与空元素同路。"""
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        """出栈到匹配的标签；匹配不上就当作多余闭合标签直接忽略。"""
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        """文本节点：原样存，空白折叠留到渲染时统一做（``pre`` 里不能折）。"""
        self._stack[-1].children.append(_Node("#text", text=data))


def _parse(html: str) -> _Node:
    """建一棵可安全遍历的树（未闭合标签由栈的残留自然收尾）。"""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def _collapse(text: str) -> str:
    """HTML 里的换行与缩进在浏览器中就是一个空格，这里保持一致。"""
    return _WHITESPACE.sub(" ", text)


def _tidy(block: str) -> str:
    """清掉一个块首尾的空行与行尾空格，并把 3+ 连续空行压成 1 个。"""
    lines = [line.rstrip() for line in block.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return _BLANK_RUNS.sub("\n\n", "\n".join(lines))


def _flush(buf: list[str], out: list[str]) -> None:
    """把攒着的行内容收成一个块（段落边界就是一次 flush）。"""
    block = _tidy("".join(buf))
    buf.clear()
    if block:
        out.append(block)


def _render(node: _Node, out: list[str], buf: list[str]) -> None:
    """渲染一个节点：块级直接进 ``out``，行内容先攒在 ``buf``。"""
    tag = node.tag

    if tag == "#text":
        buf.append(_collapse(node.text))
        return
    if tag in _SKIP_TAGS:
        return
    if tag == "br":
        # 换行留在段内：前端渲染把同段的多行拼成 <br>，语义正好对上
        buf.append("\n")
        return
    if tag == "img":
        src = node.get("src")
        if src:
            buf.append(f"![{node.get('alt') or _alt_text(node)}]({src})")
        return
    if tag == "hr":
        _flush(buf, out)
        out.append("---")
        return
    if tag in _HEADINGS:
        inner = _group(node)
        if inner:
            _flush(buf, out)
            out.append(f"{'#' * _HEADINGS[tag]} {inner}")
        return
    if tag == "a":
        inner = _group(node)
        href = node.get("href")
        if not inner and not href:
            return
        buf.append(f"[{inner or href}]({href})" if href else inner)
        return
    if tag == "code":
        # 围栏代码块由 pre 负责；裸 <code> 是行内代码
        inner = _group(node)
        if inner:
            buf.append(f"`{inner}`")
        return
    marks = _INLINE_MARKS.get(tag)
    if marks:
        inner = _group(node)
        if inner:
            buf.append(f"{marks[0]}{inner}{marks[1]}")
        return
    if tag == "blockquote":
        blocks = _blocks_of(node)
        if blocks:
            _flush(buf, out)
            inner = "\n\n".join(blocks)
            out.append(
                "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
            )
        return
    if tag == "pre":
        body = _plain(node).strip("\n")
        if body.strip():
            _flush(buf, out)
            out.append(f"```{_lang(node)}\n{body}\n```")
        return
    if tag in ("ul", "ol"):
        block = _render_list(node)
        if block:
            _flush(buf, out)
            out.append(block)
        return
    if tag == "table":
        block = _render_table(node)
        if block:
            _flush(buf, out)
            out.append(block)
        return
    if tag in _BLOCK_TAGS:
        # 容器自身不产出文字，但要先把前面积的行内容切段，保持块顺序
        _flush(buf, out)
        for child in node.children:
            _render(child, out, buf)
        _flush(buf, out)
        return
    # 未知标签（含 span/u/small 这类纯装饰行内元素）：丢掉壳，留住内容
    for child in node.children:
        _render(child, out, buf)


def _blocks_of(node: _Node) -> list[str]:
    """把子节点渲染成块列表（引用块内部要逐块加 ``>`` 前缀，不能先拼成一行）。"""
    out: list[str] = []
    buf: list[str] = []
    for child in node.children:
        _render(child, out, buf)
    _flush(buf, out)
    return out


def _group(node: _Node) -> str:
    """渲染子节点并收成一行（行内元素与标题用，段内换行压成空格）。"""
    return _collapse(" ".join(_blocks_of(node)).replace("\n", " ")).strip()


def _plain(node: _Node) -> str:
    """取纯文字（代码块用：不能有任何 Markdown 语法，也不能折行）。"""
    if node.tag == "#text":
        return node.text
    if node.tag in _SKIP_TAGS:
        return ""
    if node.tag == "br":
        return "\n"
    return "".join(_plain(child) for child in node.children)


def _lang(pre: _Node) -> str:
    """从 ``<pre><code class="language-python">`` 里摸出语言名，供围栏标注。"""
    for child in pre.children:
        if child.tag != "code":
            continue
        for token in child.get("class").split():
            if token.startswith("language-"):
                return token[len("language-") :]
            if token.startswith("lang-"):
                return token[len("lang-") :]
    return ""


def _alt_text(node: _Node) -> str:
    """图片没写 alt 时用文件名兜底，至少搜索结果里有个可读的指代。"""
    name = node.get("src").rsplit("/", 1)[-1]
    return name or "image"


def _render_list(node: _Node, level: int = 0) -> str:
    """列表转 Markdown：嵌套靠缩进，``li`` 里的块内容折到同一项下。"""
    lines: list[str] = []
    index = 0
    for item in node.children:
        if item.tag != "li":
            # 浏览器会把列表里的裸文本也当一项，这里跟一个同样的宽松处理
            wrapper = _Node("li")
            wrapper.children = [item]
            item = wrapper
        index += 1
        marker = f"{index}. " if node.tag == "ol" else "- "
        indent = "  " * level
        nested: list[str] = []
        body: list[str] = []
        item_buf: list[str] = []
        for child in item.children:
            if child.tag in ("ul", "ol"):
                block = _render_list(child, level + 1)
                if block:
                    nested.append(block)
                continue
            _render(child, body, item_buf)
        _flush(item_buf, body)
        text = _collapse(" ".join(_tidy(b) for b in body).replace("\n", " ")).strip()
        if not text and not nested:
            continue
        lines.append(f"{indent}{marker}{text}")
        lines.extend(nested)
    return "\n".join(lines)


def _render_table(node: _Node) -> str:
    """表格转管道表；没有表头行时补一行空表头（Markdown 表格必须有分隔行）。"""
    rows: list[list[str]] = []
    for tr in _descendants(node, "tr"):
        cells = [_group(td).replace("|", r"\|") for td in _children(tr, ("td", "th"))]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    body = [row + [""] * (width - len(row)) for row in rows]
    out = ["| " + " | ".join(body[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    out += ["| " + " | ".join(row) + " |" for row in body[1:]]
    return "\n".join(out)


def _children(node: _Node, tags: tuple[str, ...]) -> list[_Node]:
    """按标签名挑直接子节点（表格取 tr/td/th 用，跳过 tbody/thead 这类壳）。"""
    return [child for child in node.children if child.tag in tags]


def _descendants(node: _Node, tag: str) -> list[_Node]:
    """深度优先找后代（``tr`` 常藏在 ``tbody`` 里，光看直接子节点会漏）。"""
    found: list[_Node] = []
    for child in node.children:
        if child.tag == tag:
            found.append(child)
        found.extend(_descendants(child, tag))
    return found


def html_to_markdown(html: str) -> str:
    """把一段 HTML 转成 Markdown 源码（块之间空一行，顺序与文档一致）。"""
    out: list[str] = []
    buf: list[str] = []
    root = _parse(html)
    for child in root.children:
        _render(child, out, buf)
    _flush(buf, out)
    return "\n\n".join(out).strip()


def normalize_content(content: str, fmt: str = "text") -> str:
    """按录入格式把正文归一化成 Markdown 源码。

    ``text`` / ``markdown`` 只统一换行（分块器按换行打包，留 CRLF 会把整篇
    挤成一段）；``html`` 先转 Markdown 再入库，向量与检索看到的都是干净文本。
    """
    if fmt == "html":
        return html_to_markdown(content)
    return content.replace("\r\n", "\n").replace("\r", "\n").strip()


def stored_format(fmt: str) -> str:
    """请求格式落到库里的格式：``html`` 转完就是 Markdown，认不出的一律算纯文本。"""
    if fmt in ("markdown", "html"):
        return "markdown"
    return "text"


_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_IMG_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_NUMBER_RE = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_QUOTE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)
_RULE_RE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$", re.MULTILINE)
_MARKS_RE = re.compile(r"(\*\*|__|~~|[*_`])")
_BLANK_LINES = re.compile(r"\n+")
_WIDE_SPACES = re.compile(r"[ \t]{2,}")


def strip_markdown(text: str) -> str:
    """剥掉 Markdown 语法符号，只留可读文字（列表摘要用）。

    取「写给人看的那部分」：链接留锚文本、图片留 alt、围栏里留代码正文。
    不追求渲染级精确（行首 ``#`` 与强调符号在正文里出现会被误剥），
    摘要只看前一百多字，够用即可。
    """
    body = _FENCE_RE.sub(lambda m: m.group(1), text)
    body = _IMG_RE.sub(lambda m: m.group(1) or "图片", body)
    body = _LINK_RE.sub(r"\1", body)
    body = _HEADING_RE.sub("", body)
    body = _BULLET_RE.sub("", body)
    body = _NUMBER_RE.sub("", body)
    body = _QUOTE_RE.sub("", body)
    body = _RULE_RE.sub("", body)
    body = body.replace("|", " ")
    body = _MARKS_RE.sub("", body)
    body = _WIDE_SPACES.sub(" ", body)
    return _BLANK_LINES.sub("\n", body).strip()
