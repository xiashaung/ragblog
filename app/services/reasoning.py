"""把「思考过程」从模型正文里分出来。

深度思考模型给思考内容有两种路子，两条都支持：

1. **独立字段**：``delta.reasoning_content`` / ``delta.reasoning``（DeepSeek-R1、
   vLLM 的 ``--reasoning-parser``、SiliconFlow、Xinference 都这么吐）。这条在
   ``llm.py`` 里直接读，不经过本模块。
2. **混在正文里的标签**：```` / ````（Qwen / GLM
   等把思考写进 ``content``，或端点没开 reasoning 分路时会退化成这种形态）。
   这条由本模块的 :class:`ThinkSplitter` 在文本流上拆。

为什么不能逐块跑正则：内容是一小段一小段到的，标签会被切在任意两块之间
（实测过 ``<thi`` + ``nk>`` 这类切法），逐块匹配必然漏掉跨界的那一半。所以这里
做成有状态的分段器：能安全外发的才返回，可能是半个标签的尾巴攒住等下一块
（与 ``chat_tools.CallMarkupScrubber`` 属于同一类问题，各自独立以便阅读）。

误判防线：孤立出现的开标签不当思考块处理（模型在讲 XML/代码时也会写
````），先把标签本身攒着，等到真看到闭合标签才认领它是思考；等不到就
在 :meth:`ThinkSplitter.close` 里原样还原成正文。唯一的例外是流开头就是
思考却没带闭合标签（被 ``max_tokens`` 截断），此时整段按思考收下——
这比把用户要看的文字整段吞掉更安全。
"""

from __future__ import annotations

import re

# 开/闭标签：容忍 ``< think >`` 这种插空格的写法（有些模板会加空白）
_THINK_OPEN_RE = re.compile(r"<\s*(?:think|thinking)\s*>", re.I)
_THINK_CLOSE_RE = re.compile(r"<\s*/\s*(?:think|thinking)\s*>", re.I)

# 可能是半个标签的候选：缓冲结尾落在这些的前缀上时要攒住等下一块
_MARKER_CANDIDATES = ("", "", "<thinking>", "</thinking>")
# 标签最长 12 字符（``<thinking>``），留 24 的扫描窗口与 scrubber 同一量级
_MAX_TAIL_SCAN = 24

_OUT, _IN_THINK = 0, 1

#: :meth:`ThinkSplitter.feed` 片段的第一段取值
BODY = "body"
THINK = "think"


def _hold_back(buf: str) -> int:
    """缓冲末尾有多少字符可能是「没写完的思考标签」，需要先攒住。"""
    low = buf.lower()
    for i in range(len(low) - 1, max(len(low) - _MAX_TAIL_SCAN, -1), -1):
        if low[i] != "<":
            continue
        tail = low[i:]
        if any(candidate.startswith(tail) for candidate in _MARKER_CANDIDATES):
            return len(low) - i
    return 0


class ThinkSplitter:
    """把逐块到达的文本切成 ``(BODY | THINK, 文本)`` 片段，跨块标签也能正确归属。

    用法：每块调 :meth:`feed`（返回此刻可安全外发的片段），流结束后调 :meth:`close`
    结算缓冲。:attr:`saw_think` 表示到目前为止是否真的认定过思考内容。
    """

    def __init__(self) -> None:
        """初始为「正文状态 + 空缓冲」，几个暂存位一并清零。"""
        #: 是否认定过思考内容，供调用方判断这段回答有没有思考
        self.saw_think = False
        self._buf = ""
        self._state = _OUT
        # 见到开标签后先挂在这里：确认有闭合才转成 THINK，否则还原成 BODY
        self._pending_open = ""
        self._pending_think = ""
        #: 是否曾经攒过思考片段（用于区分「流开头就是思考」与「孤立开标签」）
        self._ever_had_think_content = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        """喂一块流式内容，返此刻可安全外发的 ``(类型, 文本)`` 片段。"""
        self._buf += text
        return self._drain(final=False)

    def close(self) -> list[tuple[str, str]]:
        """结算缓冲：未闭合的思考块按思考收下，孤立的开标签还原成正文。"""
        out = self._drain(final=True)
        if self._state == _IN_THINK:
            # 没找到闭合标签：
            # - 如果曾经攒过任何字符（包括流开头就是思考、或模型在讲 XML），整段按思考收下；
            # - 否则纯孤立开标签（只有 ```` 一个标签），还原成正文。
            text = self._pending_open + self._pending_think + self._buf
            self._pending_open = self._pending_think = self._buf = ""
            if text:
                kind = THINK if self._ever_had_think_content else BODY
                out.append((kind, text))
            return out
        if self._pending_open:
            out.append((BODY, self._pending_open))
            self._pending_open = ""
        if self._buf:
            out.append((BODY, self._buf))
            self._buf = ""
        return out

    # ---- 内部 ----

    def _drain(self, *, final: bool) -> list[tuple[str, str]]:
        """状态机主循环：从缓冲里一段段取正文/思考，直到没有确定性进展。

        ``final=True``（流已结束）时不再预留尾巴：没下一块了，半截标签只能当正文发。
        未确认闭合前先攒在 ``_pending_*`` 里，所以孤立开标签最后能还原成正文。
        """
        out: list[tuple[str, str]] = []
        while True:
            if self._state == _OUT:
                match = _THINK_OPEN_RE.search(self._buf)
                if match is None:
                    safe = len(self._buf) - (0 if final else _hold_back(self._buf))
                    if safe:
                        out.append((BODY, self._buf[:safe]))
                        self._buf = self._buf[safe:]
                    break
                if match.start():
                    out.append((BODY, self._buf[: match.start()]))
                self._pending_open = self._buf[match.start() : match.end()]
                self._buf = self._buf[match.end() :]
                self._pending_think = ""
                self._state = _IN_THINK
                continue

            match = _THINK_CLOSE_RE.search(self._buf)
            if match is None:
                # 没找到闭合标签：攒着等下一块或 close() 再定性
                keep = 0 if final else _hold_back(self._buf)
                safe = len(self._buf) - keep
                if safe:
                    self._pending_think += self._buf[:safe]
                    self._buf = self._buf[safe:]
                    # 只要攒过任何字符，就标记为「曾经有思考内容」
                    if safe > 0:
                        self._ever_had_think_content = True
                break
            emit = self._pending_think + self._buf[: match.start()]
            self._buf = self._buf[match.end() :]
            self._state = _OUT
            # 本块已确认：标签本身不再需要退回正文，不清会在 close 时漏个 ```` 到尾部
            self._pending_open = ""
            if emit or self._pending_think:
                out.append((THINK, emit))
                self.saw_think = True
        return out


def split_completed(text: str) -> tuple[str, str]:
    """一次性文本（非流式端点把整段答案放在收尾块里）→ ``(思考，正文)``。"""
    splitter = ThinkSplitter()
    parts = splitter.feed(text) + splitter.close()
    think = "".join(t for k, t in parts if k == THINK)
    body = "".join(t for k, t in parts if k == BODY)
    return think, body


__all__ = ["BODY", "THINK", "ThinkSplitter", "split_completed"]
