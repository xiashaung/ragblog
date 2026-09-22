"""联网工具链路：搜索源解析/降级、SSRF 拦截、以及「生成→工具→再生成」循环。

约定：

- 循环逻辑用**脚本化假后端**测，不碰真实模型与真实搜索引擎（联网结果不稳定，
  用它做断言等于把测试挂在别人家的 HTML 上）；只有 Bing 解析用固定 HTML 片段测。
- 端点侧只验「工具事件按 NDJSON 顺序透出」这一层契约，内容正确性交给单元层。
"""

from __future__ import annotations

import json
from http import HTTPStatus

import pytest

from app.schemas.ai import (
    ChatChunk,
    ChatMessage,
    Provider,
    ToolCall,
    ToolFunction,
    Usage,
    WebSource,
)
from app.services import chat_tools, web_search


def _tool_call_chunk(query: str = "北京今天天气") -> ChatChunk:
    return ChatChunk(
        done=True,
        usage=Usage(prompt_tokens=100, completion_tokens=8, total_tokens=108),
        tool_calls=[
            ToolCall(
                id="call_1",
                function=ToolFunction(name="web_search", arguments=json.dumps({"query": query})),
            )
        ],
    )


class ScriptedBackend:
    """按脚本逐轮换剧本，并记下每轮收到的参数（用于断言 tools/tool_choice）。"""

    # 真后端的 provider 是 Provider 枚举（路由层会读 .value），假后端也得一致
    provider = Provider.MOCK

    def __init__(self, rounds: list[list[ChatChunk]]) -> None:
        self.rounds = rounds
        self.seen: list[dict] = []

    async def chat_stream(self, messages, **kwargs):
        self.seen.append({"messages": list(messages), **kwargs})
        index = min(len(self.seen) - 1, len(self.rounds) - 1)
        for chunk in self.rounds[index]:
            yield chunk


def _answer(text: str, tokens: int) -> list[ChatChunk]:
    """一轮「正常回答」的剧本：正文块 + 带用量的收尾块。"""
    return [
        ChatChunk(content=text),
        ChatChunk(
            done=True,
            finish_reason="stop",
            usage=Usage(
                prompt_tokens=tokens, completion_tokens=tokens, total_tokens=tokens * 2
            ),
        ),
    ]


async def _collect(backend, **overrides) -> tuple[list[ChatChunk], ScriptedBackend]:
    kwargs = {
        "messages": [_user_msg("北京今天天气怎么样")],
        "temperature": 0.3,
        "max_tokens": 256,
    }
    kwargs.update(overrides)
    out = [c async for c in chat_tools.stream_with_tools(backend, **kwargs)]
    return out, backend


def _user_msg(text: str):
    return ChatMessage(role="user", content=text)


def _msgs(*items: tuple[str, str]) -> list[ChatMessage]:
    return [ChatMessage(role=role, content=text) for role, text in items]


# ---------------- Bing 解析 ----------------

BING_HTML = """
<html><body><ol id="b_results">
<li class="b_algo"><h2><a href="https://example.com/a1">标题一</a></h2>
<div><p>这是第一条摘要，带 <b>标签</b> 需要被清掉。</p></div></li>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;p=xyz&amp;u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9hMiZlcT14&amp;ori=1">标题二</a></h2>
<div><p>第二条摘要。</p></div></li>
<li class="b_algo"><h2><a href="javascript:void(0)">脏链接</a></h2></li>
<li class="b_algo"><h2>没有链接</h2></li>
</ol></body></html>
"""


def test_parse_bing_extracts_title_url_snippet():
    results = web_search._parse_bing(BING_HTML, 5)
    assert [r.title for r in results] == ["标题一", "标题二"]
    assert results[0].url == "https://example.com/a1"
    assert results[0].snippet == "这是第一条摘要，带 标签 需要被清掉。"
    # base64 包起来的跳转链接要还原成真实目标站，否则 fetch_page 会去抓跳转页
    assert results[1].url == "https://example.com/a2&eq=x"


def test_parse_bing_respects_count():
    assert len(web_search._parse_bing(BING_HTML, 1)) == 1


def test_resolve_source_degrades_without_key(monkeypatch):
    settings = web_search.get_settings()
    monkeypatch.setattr(settings, "web_search_provider", "tavily")
    monkeypatch.setattr(settings, "web_search_api_key", "")
    source, note = web_search.resolve_source()
    assert source == "bing"
    assert "APP_WEB_SEARCH_API_KEY" in note

    monkeypatch.setattr(settings, "web_search_api_key", "tvly-test")
    assert web_search.resolve_source() == ("tavily", "")


async def test_search_caches_and_caps_count(monkeypatch):
    calls: list[tuple[str, int]] = []

    async def fake(query, count):
        calls.append((query, count))
        # 相关性闸门会丢掉与查询词零匹配的结果，假数据得带上查询词
        return [web_search.SearchResult(title=f"{query} 的结果", url="https://e.com", snippet="s")]

    monkeypatch.setattr(web_search, "_search_bing", fake)
    monkeypatch.setattr(web_search, "resolve_source", lambda: ("bing", ""))
    web_search.reset_cache()

    limit = web_search.get_settings().web_search_max_results
    await web_search.search("同一个查询", count=99)
    await web_search.search("同一个查询", count=99)
    assert calls == [("同一个查询", limit)], "条数被上限夹住，且第二次应命中缓存"

    web_search.reset_cache()
    with pytest.raises(web_search.SearchError):
        await web_search.search("   ")


async def test_zero_match_results_are_dropped_and_query_narrowed(monkeypatch):
    """Bing 对多词中文查询会静默降级（实测返回「赛」字字典页），零匹配结果不能喂给模型。

    闸门与改写是一套的：丢掉垃圾后如果直接报“搜不到”，模型就会凭记忆编；
    收窄到最有区分度的两个词再试一次，才救得回这一轮。
    """
    seen: list[str] = []

    async def fake(query, count):
        seen.append(query)
        if len(query.split()) > 2:
            return [
                web_search.SearchResult(
                    title="赛 - 汉语字典", url="https://zdic.net/xx", snippet="赛，逐也。"
                )
            ]
        return [
            web_search.SearchResult(
                title="华为消息", url="https://b.example", snippet="只命中一个词"
            ),
            web_search.SearchResult(
                title="赛力斯与华为的合作", url="https://a.example", snippet="两个词都在"
            ),
        ]

    monkeypatch.setattr(web_search, "_search_bing", fake)
    monkeypatch.setattr(web_search, "resolve_source", lambda: ("bing", ""))
    web_search.reset_cache()

    results = await web_search.search("赛力斯 华为 解绑 谣言")
    assert len(seen) == 2, "第一次零匹配后应收窄重试一次"
    assert len(seen[1].split()) == 2, "收窄到两个最有区分度的词"
    # 命中多的排前面：模型第一眼要看到强相关那条
    assert [r.url for r in results] == ["https://a.example", "https://b.example"]


async def test_interior_bigram_polyseme_noise_is_dropped(monkeypatch):
    """问「中秋高速免费」时模型二轮会把政策名「重大节假日」当词搜，结果全涌出「重庆大学」。

    实测：「重大」是「重大节假日」的内部 2-gram，又恰好是重庆大学的简称，
    旧闸门“命中任意 2-gram 就放行”会把这些同词不同义的页面喂给模型。
    新闸门要求“命中完整词 或 同时命中 >=2 个 2-gram”，单蹭一个碎片不再算数。
    """

    async def fake(query, count):
        return [
            # 只命中“重大”一个 2-gram（重大是重庆大学简称）：同词不同义的噪声，必须被丢掉
            web_search.SearchResult(
                title="重庆大学", url="https://cqu.edu.cn", snippet="简称重大"
            ),
            web_search.SearchResult(
                title="国家重大建设项目库", url="https://ndrc.gov.cn", snippet="重大项目管理"
            ),
            # 真相关：命中完整词“高速免费”
            web_search.SearchResult(
                title="高速免费通行政策", url="https://gov.example/jiedia", snippet="节假日免费"
            ),
        ]

    monkeypatch.setattr(web_search, "_search_bing", fake)
    monkeypatch.setattr(web_search, "resolve_source", lambda: ("bing", ""))
    web_search.reset_cache()

    results = await web_search.search("重大节假日 高速免费")
    assert [r.url for r in results] == ["https://gov.example/jiedia"], "只留真相关那条"


def test_gate_terms_splits_full_terms_from_interior_bigrams():
    """闸门拆两组串：完整词与内部 2-gram 不同对待（前者命中即放行，后者需多个共现）。"""
    full, bigrams = web_search._gate_terms(web_search.terms_of("重大节假日 高速免费"))
    assert full == ["重大节假日", "高速免费"]
    assert "重大" in bigrams and "高速" in bigrams
    # 两字短词不拆 2-gram（它本身就是完整词，无需再降粒度）
    assert web_search._gate_terms(["北京"]) == (["北京"], [])


def test_refine_query_handles_the_model_copying_the_whole_sentence():
    """实跑形态：模型把用户原句当关键词，既没空格也搜不到东西。

    多词查询走“取最有区分度的两个词”，单串连写走“去口语尾巴 + 截短”；
    两条都必须在 2 个候选内收敛，不能拿改写当借口反复打搜索引擎。
    """
    query = "赛力斯离开华为之后会怎么样"
    terms = web_search.terms_of(query)
    assert len(terms) == 1, "连写句只有一个词，收窄逻辑必须能接住它"
    cand = web_search._refine_query(query, terms)
    assert cand[0] == "赛力斯离开华为", cand
    assert all(c != query for c in cand)

    multi_q = "赛力斯 华为 解绑 谣言"
    multi = web_search._refine_query(multi_q, web_search.terms_of(multi_q))
    assert multi[0] == "赛力斯 华为", multi

    # 本来就像关键词的（短、没口语尾巴）不该被乱改
    short = "苏州天气"
    assert web_search._refine_query(short, web_search.terms_of(short)) == []


# ---------------- SSRF 拦截 ----------------

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8088/v1/chat",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://[::1]/x",
        "http://wiki.internal/page",
        "file:///etc/passwd",
    ],
)
def test_assert_public_url_blocks_internal(url):
    with pytest.raises(ValueError):
        chat_tools.assert_public_url(url)


def test_assert_public_url_allows_public_and_opt_in_private():
    assert chat_tools.assert_public_url("https://example.com/a")
    # 显式放开（内网 wiki 等场景）才允许私网地址
    assert chat_tools.assert_public_url("http://127.0.0.1", allow_private=True)


async def test_fetch_page_tool_refuses_internal_url_without_network():
    result = await chat_tools.execute("fetch_page", '{"url":"http://127.0.0.1:8011/api/v1/health"}')
    assert result.ok is False
    assert result.error == "blocked"
    assert "内网" in result.content


async def test_unknown_tool_fails_softly():
    result = await chat_tools.execute("drop_table", "{}")
    assert result.ok is False
    assert result.error == "unknown_tool"


def test_parse_args_survives_broken_json():
    """小模型偶尔吐半截 JSON，能捞出关键词就别丢这一轮。"""
    assert chat_tools._parse_args('{"query": "北京天气", "count": 3}')["query"] == "北京天气"
    assert chat_tools._parse_args('{"query": "上海天气"')["query"] == "上海天气"
    assert chat_tools._parse_args("") == {}


# ---------------- 工具循环 ----------------


async def test_loop_runs_tool_then_answers(monkeypatch):
    async def fake_search(query, count=None):
        return [
            web_search.SearchResult(
                title="北京天气", url="https://weather.example/ny", snippet="晴，24 度"
            )
        ]

    monkeypatch.setattr(web_search, "search", fake_search)
    rounds = [
        [_tool_call_chunk()],
        _answer("北京今天晴，24 度", 27),
    ]
    # 用不带时效词的问题：只测“模型自己请求工具”这条路，预检索另测
    chunks, backend = await _collect(
        ScriptedBackend(rounds),
        web_mode="auto",
        messages=_msgs(("user", "讲讲北京的历史城区")),
    )

    events = [c.tool for c in chunks if c.tool]
    assert [e.phase for e in events] == ["call", "result"]
    assert events[0].detail == "搜索：北京今天天气"
    assert events[1].ok is True
    assert events[1].sources[0].url == "https://weather.example/ny"

    text = "".join(c.content for c in chunks if c.content)
    assert text == "北京今天晴，24 度"
    tail = chunks[-1]
    assert tail.done is True
    # 两轮用量累加：只报最后一轮会让前端显示的 token 数严偏小
    assert tail.usage.total_tokens == 108 + 54
    # tool_calls 是内部信号，不该漏给浏览器
    assert all(c.tool_calls is None for c in chunks)

    # 第二轮必须带上工具回合：assistant(tool_calls) + tool(结果)，否则模型看不到搜索内容
    second = backend.seen[1]["messages"]
    assert [m.role for m in second] == ["system", "user", "assistant", "tool"]
    assert second[3].tool_call_id == "call_1"
    assert "weather.example" in second[3].content
    assert backend.seen[0]["tools"] == chat_tools.TOOL_SPECS


async def test_forced_mode_presearches_before_asking_model(monkeypatch):
    """必查＝服务端先搜：实测 omlx 呚掉 tool_choice=required，所以不能指望端点。"""

    async def fake_search(query, count=None):
        return [web_search.SearchResult(title="t", url="https://e.com", snippet="s")]

    monkeypatch.setattr(web_search, "search", fake_search)
    rounds = [
        [_tool_call_chunk("强制查")],
        _answer("答", 1),
    ]
    chunks, backend = await _collect(
        ScriptedBackend(rounds),
        web_mode="forced",
        messages=_msgs(("user", "讲讲北京的历史城区")),
    )
    events = [c.tool for c in chunks if c.tool]
    assert events[0].detail == "预检索：讲讲北京的历史城区"
    assert events[0].phase == "call" and events[1].phase == "result"
    # forced 也只给端点 auto：required 在思考模式端点上直接 400
    assert all(call["tool_choice"] == "auto" for call in backend.seen)
    # 搜完的结果以工具回合的形式喂进去，模型看得到，不需要再自己请求一次
    first = backend.seen[0]["messages"]
    assert [m.role for m in first] == ["system", "user", "assistant", "tool"]
    assert first[3].tool_call_id == "call_pre_1"
    assert backend.seen[0]["tools"] == chat_tools.TOOL_SPECS, "工具仍然挂着，模型可追加检索"
    assert chunks[-1].done is True


async def test_off_mode_declares_no_tools():
    chunks, backend = await _collect(
        ScriptedBackend([[ChatChunk(content="只凭记忆答"), ChatChunk(done=True)]]),
        web_mode="off",
    )
    assert backend.seen[0]["tools"] is None
    assert not any(c.tool for c in chunks)
    # 不联网时也不该塞工具提示词，否则白占 token
    assert [m.role for m in backend.seen[0]["messages"]] == ["user"]


async def test_json_mode_wins_over_tools():
    """要结构化输出时不挂工具：两者同时传会被兼容端点拒绝，也比默默丢 JSON 模式更好预测。"""
    chunks, backend = await _collect(
        ScriptedBackend([[ChatChunk(content='{}'), ChatChunk(done=True)]]),
        web_mode="forced",
        response_format={"type": "json_object"},
    )
    assert backend.seen[0]["tools"] is None
    assert backend.seen[0]["response_format"] == {"type": "json_object"}
    assert [m.role for m in backend.seen[0]["messages"]] == ["user"]
    assert chunks[-1].done is True


async def test_loop_stops_at_round_limit_and_forces_answer(monkeypatch):
    hits: list[str] = []

    async def fake_search(query, count=None):
        hits.append(query)
        # 每批结果都得不同：URL 集重复会先触发“换词重搜熔断”，测不到轮数上限那条路
        return [
            web_search.SearchResult(
                title="t", url=f"https://e.com/{query}", snippet="s"
            )
        ]

    monkeypatch.setattr(web_search, "search", fake_search)
    # 预算和轮数两道闸先撞哪道就会改写后面的计数：这条路径要测轮数闸，放飞预算
    monkeypatch.setattr(chat_tools, "MAX_SEARCH_CALLS", 10**3)
    max_rounds = chat_tools.get_settings().web_search_max_rounds
    # 每轮都坚持要工具：模拟模型卡在「再搜一下」的循环里
    rounds = [[_tool_call_chunk(f"q{i}")] for i in range(max_rounds + 3)]
    # 用不带时效词的问题：预检索会多占一轮并改写轮数计算，这条线要测纯轮数耗尽
    chunks, backend = await _collect(
        ScriptedBackend(rounds),
        web_mode="auto",
        messages=_msgs(("user", "讲讲北京的历史城区")),
    )

    assert len(hits) == max_rounds, "上限后必须停止执行工具"
    last = backend.seen[-1]
    assert last["tools"] is None, "最后一轮要把工具撤掉才能强制作答"
    assert chat_tools.ROUND_LIMIT_HINT in [m.content for m in last["messages"][-1:]]
    assert chunks[-1].done is True


async def test_duplicate_search_stops_rounds_and_forces_answer(monkeypatch):
    """换词重搜拿到同一批结果时就该停：实测它会连搜三轮把额度烧光还答不上来。"""
    hits: list[str] = []

    async def same_results(query, count=None):
        hits.append(query)
        return [web_search.SearchResult(title="t", url="https://e.com", snippet="s")]

    monkeypatch.setattr(web_search, "search", same_results)
    # 第二轮结果与预检索那批完全相同，就该强制作答：后面的“再搜一次”剧本不该被走到
    rounds = [
        [_tool_call_chunk("苏州实时天气")],
        _answer("晴，24 度", 4),
        [_tool_call_chunk("苏州今天天气 实时")],
    ]
    chunks, backend = await _collect(ScriptedBackend(rounds), web_mode="auto")

    assert len(hits) == 2, "预检索一次 + 模型搜一次，之后不许再打搜索引擎"
    assert len(backend.seen) == 2, "熔断后下一轮就是作答轮"
    tool_events = [c.tool for c in chunks if c.tool]
    assert [e.phase for e in tool_events] == ["call", "result", "call", "result"]
    # 熔断信号要贴在重复那批结果后面（离结果越近越管用）
    last_tool = backend.seen[-1]["messages"][-2]
    assert last_tool.role == "tool"
    assert chat_tools.REPEAT_STOP_HINT in last_tool.content
    # 作答轮只靠最后那条 system 强制作答（重复提示已在工具结果里，不堆第二条）
    assert [m.role for m in backend.seen[-1]["messages"][-2:]] == ["tool", "system"]
    assert backend.seen[-1]["messages"][-1].content == chat_tools.ROUND_LIMIT_HINT
    assert backend.seen[-1]["tools"] is None
    assert "".join(c.content for c in chunks if c.content) == "晴，24 度"


async def test_search_budget_blocks_extra_searches(monkeypatch):
    """换词搜不同话题时结果签名拦不住（每批 URL 都不同），硬闸只能是搜索次数预算。

    实跑的坑：思考模式云端模型会「北京天气」→「北京气温趋势」→「北京历史」
    一路搜下去，每次结果都“新鲜”，轮数烧光也没给答案。
    """
    hits: list[str] = []

    async def ever_new(query, count=None):
        hits.append(query)
        return [web_search.SearchResult(title="t", url=f"https://e.com/{query}", snippet="s")]

    monkeypatch.setattr(web_search, "search", ever_new)
    # 预检索占一次 + 模型首轮占一次（共 2=预算），第二轮再要搜就被拦；
    # 拦下后下一轮就是强制作答轮（exhausted），剧本里正文要放在这一轮
    rounds = [
        [_tool_call_chunk("北京天气")],
        [_tool_call_chunk("北京气温趋势")],
        _answer("晴，24 度", 5),
    ]
    chunks, backend = await _collect(ScriptedBackend(rounds), web_mode="auto")

    assert len(hits) == chat_tools.MAX_SEARCH_CALLS, "搜索引擎最多被打这么多次"
    # 模型调用轮：预检索那轮不占它，预算内两次搜索各一轮，再加发现超预算的那一轮
    assert len(backend.seen) == chat_tools.MAX_SEARCH_CALLS + 1, "预算内两轮 + 撞闸一轮"
    last_tool = backend.seen[-1]["messages"][-2]
    assert last_tool.role == "tool"
    assert last_tool.content == chat_tools.SEARCH_BUDGET_STOP
    # 被拦下的那次不只能见地告诉前端「没执行」，下一轮还要撤工具强制作答
    blocked = [c.tool for c in chunks if c.tool and c.tool.phase == "result"][-1]
    assert blocked.ok is False and "上限" in blocked.error
    assert backend.seen[-1]["tools"] is None
    assert "".join(c.content for c in chunks if c.content) == "晴，24 度"


def _named_call(name: str, args: dict | None = None) -> ChatChunk:
    call = _tool_call_chunk()
    call.tool_calls[0].function.name = name
    call.tool_calls[0].function.arguments = json.dumps(args or {}, ensure_ascii=False)
    return call


async def test_non_search_tools_do_not_eat_search_budget(monkeypatch):
    """预算数的是 web_search 次数：fetch_page / 未知工具占的是轮数，不是搜索额度。"""
    hits: list[str] = []

    async def fake_search(query, count=None):
        hits.append(query)
        return [web_search.SearchResult(title="t", url=f"https://e.com/{query}", snippet="s")]

    monkeypatch.setattr(web_search, "search", fake_search)
    rounds = [
        [_tool_call_chunk("北京天气")],
        [_named_call("browse_history")],
        [_named_call("browse_history")],
        _answer("晴，24 度", 5),
    ]
    chunks, backend = await _collect(ScriptedBackend(rounds), web_mode="auto")

    assert len(hits) == 2, "预检索 + 模型一次，非搜索工具不该把它顶到超预算"
    assert len(backend.seen) == 4, "三次工具回合都正常执行，没触发预算熔断"


async def test_tool_failure_is_reported_to_model_not_raised(monkeypatch):
    async def broken_search(query, count=None):
        raise web_search.SearchError("tavily 返回 HTTP 432：额度用尽")

    monkeypatch.setattr(web_search, "search", broken_search)
    rounds = [
        [_tool_call_chunk()],
        [ChatChunk(content="查不到，我按常识说"), ChatChunk(done=True)],
    ]
    chunks, _ = await _collect(ScriptedBackend(rounds), web_mode="auto")
    result_event = [c.tool for c in chunks if c.tool][-1]
    assert result_event.phase == "result"
    assert result_event.ok is False
    assert "额度用尽" in result_event.error
    assert "".join(c.content for c in chunks if c.content) == "查不到，我按常识说"


# ---------------- 端点契约 ----------------


def _lines(body: str) -> list[dict]:
    return [json.loads(line) for line in body.splitlines() if line.strip()]


async def test_chat_endpoint_streams_tool_events(client, monkeypatch):
    """走一遍真实 HTTP：工具调用与命中链接要以 NDJSON 事件到达前端。"""

    async def fake_search(query, count=None):
        return [
            web_search.SearchResult(title="天气", url="https://w.example", snippet="晴"),
        ]

    monkeypatch.setattr(web_search, "search", fake_search)
    backend = ScriptedBackend(
        [
            [_tool_call_chunk()],
            _answer("今天晴", 7),
        ]
    )
    monkeypatch.setattr("app.routers.ai.get_llm", lambda provider=None: backend)

    res = await client.post(
        "/api/v1/ai/chat",
        json={"messages": [{"role": "user", "content": "今天天气"}], "stream": True},
    )
    assert res.status_code == HTTPStatus.OK, res.text
    lines = _lines(res.text)
    events = [c["tool"] for c in lines if c.get("tool")]
    # “今天天气”是时效性问题：先有一次预检索，模型又主动要了一次工具，所以是两对事件
    assert [e["phase"] for e in events] == ["call", "result", "call", "result"]
    assert events[0]["detail"] == "预检索：今天天气"
    assert events[1]["sources"][0]["url"] == "https://w.example"
    assert "".join(c.get("content") or "" for c in lines) == "今天晴"
    assert lines[-1]["done"] is True
    # 收尾块不能再带一份正文，但也不能给 null：前端拼字符串会直接 TypeError
    assert all(isinstance(c.get("content") or "", str) for c in lines)


async def test_non_stream_chat_also_supports_tools(client, monkeypatch):
    """非流式与流式共用一份循环：别让「联网」只在一条路径上可用。

    顺手拿两轮工具验证引用去重：两次搜索命中同一篇时，链接只能列一次。
    """

    async def fake_search(query, count=None):
        return [
            web_search.SearchResult(title="天气", url="https://w.example", snippet="晴"),
            web_search.SearchResult(title=query, url=f"https://w.example/{query}", snippet="新"),
        ]

    monkeypatch.setattr(web_search, "search", fake_search)
    backend = ScriptedBackend(
        [
            [_tool_call_chunk("北京烤鸭的历史")],
            [_tool_call_chunk("北京小吃分布")],
            _answer("晴，24 度", 3),
        ]
    )
    monkeypatch.setattr("app.routers.ai.get_llm", lambda provider=None: backend)

    res = await client.post(
        "/api/v1/ai/chat",
        json={"messages": [{"role": "user", "content": "说说北京饮食"}]},
    )
    assert res.status_code == HTTPStatus.OK, res.text
    body = res.json()["data"]
    assert body["content"] == "晴，24 度"
    assert body["usage"]["total_tokens"] == 108 + 108 + 6  # 三轮模型调用累加
    # 两次搜索各带一条新链接（问题不带时效词、不触发预检索，不占搜索预算）
    assert [s["url"] for s in body["web_sources"]] == [
        "https://w.example",
        "https://w.example/北京烤鸭的历史",
        "https://w.example/北京小吃分布",
    ]


async def test_backend_without_tools_support_still_streams(client, monkeypatch):
    """未实现 tools 参数的后端（如 mock）不该因为多传了关键字参数就挂。"""

    res = await client.post(
        "/api/v1/ai/chat",
        json={
            "provider": "mock",
            "messages": [{"role": "user", "content": "你好"}],
            "stream": True,
            "web_mode": "auto",
        },
    )
    assert res.status_code == HTTPStatus.OK, res.text
    lines = _lines(res.text)
    assert "".join(c.get("content", "") for c in lines).endswith("你好")
    assert lines[-1]["done"] is True


async def test_web_sources_round_trip_through_history(client, console):
    """联网引用要随回答入库并在回放时取回：否则刷新后看不出答案是查来的。"""
    headers = console["headers"]
    res = await client.post(
        "/api/v1/ai/chat/sessions", json={"title": "联网来源"}, headers=headers
    )
    session_id = res.json()["data"]["id"]

    source = {"title": "中国天气网", "url": "https://weather.example/1", "snippet": "晴"}
    res = await client.post(
        f"/api/v1/ai/chat/sessions/{session_id}/messages",
        headers=headers,
        json={
            "messages": [
                {"role": "user", "content": "北京今天天气"},
                {
                    "role": "assistant",
                    "content": "晴，24 度。",
                    "web_sources": [source],
                },
            ]
        },
    )
    assert res.status_code == HTTPStatus.OK, res.text
    stored = res.json()["data"]["messages"]
    assert stored[1]["web_sources"] == [source]
    # 提问那条没来源：要是 None，前端每处渲染都得多一层判空
    assert stored[0]["web_sources"] == []

    res = await client.get(
        f"/api/v1/ai/chat/sessions/{session_id}/messages", headers=headers
    )
    assert res.json()["data"]["messages"][1]["web_sources"] == [source]


# ---------------- auto 的触发策略 ----------------
# 同一个问题单独问会搜，前面聊过几句“我不能联网”就改口拒绝——这两件事都得兜住。


async def test_auto_forces_search_for_time_sensitive_question(monkeypatch):
    """时效性问题即使模型不主动请求，也由服务端预检索兜住（实测它会直接凭记忆作答）。"""

    async def fake_search(query, count=None):
        assert query == "苏州今天天气怎么样"
        return [
            web_search.SearchResult(title="苏州天气", url="https://w.example", snippet="中雨")
        ]

    monkeypatch.setattr(web_search, "search", fake_search)
    # 假后端一轮就给出答案（不请求工具）：旧写法下这就会得到“我无法联网”式的回答
    chunks, backend = await _collect(
        ScriptedBackend([_answer("苏州今天中雨，16°C", 1)]),
        web_mode="auto",
        messages=_msgs(("user", "苏州今天天气怎么样")),
    )
    events = [c.tool for c in chunks if c.tool]
    assert [e.phase for e in events] == ["call", "result"]
    assert events[0].detail == "预检索：苏州今天天气怎么样"
    assert events[1].sources[0].url == "https://w.example"
    first = backend.seen[0]["messages"]
    assert [m.role for m in first] == ["system", "user", "assistant", "tool"]
    assert "中雨" in first[3].content
    assert "".join(c.content for c in chunks if c.content) == "苏州今天中雨，16°C"


async def test_auto_does_not_force_for_small_talk():
    """带“今天”的翻译请求不该去搜：否则白烧一次额度与上千 token。"""
    chunks, backend = await _collect(
        ScriptedBackend([[ChatChunk(content="好"), ChatChunk(done=True)]]),
        web_mode="auto",
        messages=_msgs(("user", "把这句话翻译成英文：今天天气不错")),
    )
    assert backend.seen[0]["tool_choice"] == "auto"
    assert not any(c.tool for c in chunks)


async def test_refusal_in_history_gets_corrected_but_not_forced():
    """用户怎么问不算拒绝，助手说过“我无法联网”才会污染后面每一轮。"""
    chunks, backend = await _collect(
        ScriptedBackend([[ChatChunk(content="好"), ChatChunk(done=True)]]),
        web_mode="auto",
        messages=_msgs(
            ("user", "你能联网查询吗"),
            ("assistant", "抱歉，我无法直接联网查询实时信息。"),
            ("user", "介绍一下幂等性"),
        ),
    )
    seen = backend.seen[0]["messages"]
    assert [m.role for m in seen] == ["system", "user", "assistant", "user", "system"]
    assert seen[-1].content == chat_tools.REFUSAL_CORRECTION
    # 非时效性问题依旧 auto：纠正污染只是把工具留着，不是通通逼它搜
    assert backend.seen[0]["tool_choice"] == "auto"


async def test_refusal_history_plus_time_sensitive_still_forces_search(monkeypatch):
    async def fake_search(query, count=None):
        return [web_search.SearchResult(title="t", url="https://e.com", snippet="s")]

    monkeypatch.setattr(web_search, "search", fake_search)
    _, backend = await _collect(
        ScriptedBackend([[_tool_call_chunk()], _answer("晴", 1)]),
        web_mode="auto",
        messages=_msgs(
            ("user", "你自动通过百度查询"),
            ("assistant", "抱歉，我无法自动通过搜索引擎查询。"),
            ("user", "苏州今天天气"),
        ),
    )
    # 必查意图不透传给端点：思考模式端点收 required 会 400，强制靠预检索兑现
    assert backend.seen[0]["tool_choice"] == "auto"
    first = backend.seen[0]["messages"]
    # 更正提示注入在提问之后；预检索合成的工具回合接在它后面（越靠近当前轮越管用）
    assert [m.role for m in first] == [
        "system",
        "user",
        "assistant",
        "user",
        "system",
        "assistant",
        "tool",
    ]
    assert first[4].content == chat_tools.REFUSAL_CORRECTION
    assert "https://e.com" in first[6].content


def test_needs_web_pattern_boundaries():
    assert chat_tools.needs_web("苏州今天天气")
    assert chat_tools.needs_web("现在美元汇率多少")
    assert chat_tools.needs_web("最新的发布版本修复了什么")
    # 时事类提法没有名词关键词，也得能识别出来（实测这类会被 auto 漏掉）
    assert chat_tools.needs_web("赛力斯离开了华为的渠道 会有怎么样的影响")
    assert chat_tools.needs_web("他最近的近况如何")
    assert not chat_tools.needs_web("用三句话解释什么是向量数据库")
    # 开发语境里的“现在/后续”不该被当成联网信号
    assert not chat_tools.needs_web("这段代码后续要怎么改")
    assert not chat_tools.needs_web("这个索引对查询性能的影响")
    assert not chat_tools.needs_web("你好")
    assert not chat_tools.needs_web("翻译：今天天气很好")
    assert not chat_tools.needs_web("")


def test_history_denies_web_only_reads_assistant_text():
    assert chat_tools._history_denies_web(_msgs(("assistant", "我没有联网能力")))
    # 三句全部取自实跑会话原文：带 markdown 星号、带插入语，贴着写的正则都会漏
    for real in [
        "目前我**不能直接实时联网查询**最新的互联网信息。",
        "抱歉，我**无法**自动通过百度或其他搜索引擎进行实时联网查询。",
        "我无法直接联网查询实时的天气信息。",
    ]:
        assert chat_tools._history_denies_web(_msgs(("assistant", real))), real
    assert not chat_tools._history_denies_web(
        _msgs(("user", "你能联网吗"), ("assistant", "能，我可以调用搜索工具"))
    )
    # 只谈“不能”而不涉联网，不该误判（否则任何错误致歉都会注入更正）
    assert not chat_tools._history_denies_web(_msgs(("assistant", "这段代码我不能执行")))
    # 工具回合的助手消息 content 为 None，不能把判断带崩
    tool_turn = ChatMessage(
        role="assistant",
        content=None,
        tool_calls=[
            ToolCall(id="c1", function=ToolFunction(name="web_search", arguments="{}"))
        ],
    )
    assert not chat_tools._history_denies_web([tool_turn])


# ---------------- 伪工具调用标记 ----------------
# 工具被撤掉后模型仍想调，就把调用标记当正文吐出来；不剥掉前端就是一屏 XML。
# 标记在测试里是拼出来的：写全会被外层工具当成真实调用解析掉。

def _call_lines(query: str, *, closed: bool = True) -> list[str]:
    lt = "<"
    name = "tool" + "_call"
    lines = [
        "\n" + lt + name + ">",
        "\n" + lt + "function=web_search" + ">" + "\n" + lt + "parameter=query" + ">",
        "\n" + query + "\n" + lt + "/parameter" + ">",
        "\n" + lt + "/function" + ">",
    ]
    if closed:
        lines.append("\n" + lt + "/tool_call" + ">")
    return lines


async def _exhausted(monkeypatch, tail_script: list[ChatChunk]):
    """每一轮都坚持要工具，撞上轮数上限后按 tail_script 收尾。"""

    async def fake_search(query, count=None):
        # 每轮一批新结果：撞上限靠的是轮数，不是“换词重搜拿到同一批”的熔断
        return [
            web_search.SearchResult(
                title="t", url=f"https://e.com/{query}", snippet="s"
            )
        ]

    monkeypatch.setattr(web_search, "search", fake_search)
    # 这两条路径的开关互斥：预算比轮数先到的话，看到的就是熔断而不是上限轮
    monkeypatch.setattr(chat_tools, "MAX_SEARCH_CALLS", 10**3)
    settings = chat_tools.get_settings()
    # 默认值就是 3，显式钉住：上面把预算放飞后，撞上限的必须是轮数
    max_rounds = 3
    monkeypatch.setattr(settings, "web_search_max_rounds", max_rounds)
    # 预检索不占模型的调用额度（max_rounds 已在里面 +1），所以正好是 max_rounds 轮
    # “坚持要工具”之后才撞上限，那一轮才是收尾轮（tail_script）
    rounds = [[_tool_call_chunk(f"q{i}")] for i in range(max_rounds)] + [tail_script]
    chunks, _ = await _collect(
        ScriptedBackend(rounds),
        web_mode="auto",
        # 不带时效词：预检索会多占一轮并改写轮数计算，这条路径要测的是纯轮数耗尽
        messages=_msgs(("user", "讲讲北京的历史城区")),
    )
    return "".join(c.content for c in chunks if c.content)


async def test_exhausted_round_strips_fake_tool_call(monkeypatch):
    tail = [ChatChunk(content=line) for line in _call_lines("赛力斯 华为 解绑 谣言")]
    tail.append(ChatChunk(done=True))
    text = await _exhausted(monkeypatch, tail)
    assert "<" not in text and "tool" + "_call" not in text
    # 全是标记时既不能发空气泡，也不能只丢一句“额度用完”：手里有真实来源就要交给用户
    assert "https://e.com" in text
    assert chat_tools.ROUND_LIMIT_CALLED not in text


def test_fallback_answer_prefers_real_sources_over_quota_jargon():
    """上限轮的兜底优先级：真实链接 > “还想搜” > “啥也没查到”。"""
    src = WebSource(title="赛力斯公告", url="https://a.example", snippet="")
    called = [ToolCall(id="c", function=ToolFunction(name="web_search", arguments="{}"))]
    text = chat_tools._fallback_answer(called, [src])
    assert "https://a.example" in text and "赛力斯公告" in text
    # 只列链接不下结论：没模型参与的文字不能替它编一个“现状”
    assert "没查到" not in text

    assert chat_tools._fallback_answer(called, []) == chat_tools.ROUND_LIMIT_CALLED
    assert chat_tools._fallback_answer([], []) == chat_tools.ROUND_LIMIT_EMPTY


async def test_exhausted_round_keeps_real_prose(monkeypatch):
    tail = [ChatChunk(content="根据已拿到的搜索结果，合作并未终止。\n")]
    tail += [ChatChunk(content=line) for line in _call_lines("赛力斯 最新公告")]
    tail.append(ChatChunk(done=True))
    text = await _exhausted(monkeypatch, tail)
    assert text == "根据已拿到的搜索结果，合作并未终止。"


def test_strip_tool_markup_boundaries():
    strip = chat_tools.strip_tool_markup
    call = "".join(_call_lines("北京天气"))
    assert strip(call) == ""
    assert strip("结论如下。\n\n" + call) == "结论如下。"
    # 被 max_tokens 截断时闭合标记不存在，要一路吃到末尾
    broken = "".join(_call_lines("北京天气", closed=False))
    assert strip("结论：合作仍在。\n" + broken) == "结论：合作仍在。"
    # 普通尖括号与代码示例不能误伤
    assert strip("数组 a[i]<n 这种写法") == "数组 a[i]<n 这种写法"
    assert strip("<div class=\"x\">正文</div>") == "<div class=\"x\">正文</div>"
    # 落单的半个标记也要清掉（收尾块里只剩闭合标签的形态）
    assert strip("答案。\n" + "<" + "/tool_call" + ">") == "答案。"


def _scrub(pieces: list[str]) -> tuple[str, list[tuple[str, str]]]:
    """按流式节奏喂标记，返回（外发正文, 还原出的工具调用）。"""
    scrubber = chat_tools.CallMarkupScrubber()
    out = "".join(scrubber.feed(piece) for piece in pieces) + scrubber.close()
    return out, [(c.function.name, c.function.arguments) for c in scrubber.calls]


def test_scrubber_survives_marker_split_across_chunks():
    """标记被切在任意两块之间是常态（模型一小段一小段吐），所以不能逐块跑正则。

    实测的坑：包裹标记与内层函数标记分在两块时，后者会被当成“块结束”，
    结果整块被丢空而 ``<parameter=...>`` 当正文漏到前端。
    """
    text, calls = _scrub(["结论：合作仍在。\n"] + _call_lines("北京天气"))
    assert "<" not in text and "parameter" not in text
    assert [name for name, _ in calls] == ["web_search"]
    assert "北京天气" in calls[0][1]

    whole = "结论。\n" + "".join(_call_lines("北京天气"))
    text2, calls2 = _scrub(list(whole))  # 一个字符一块
    assert "<" not in text2 and len(calls2) == 1


def test_scrubber_drops_marker_cut_by_max_tokens():
    """流正好断在标记中间：半截标记不能留在正文里（否则前端看到 ``<tool_ca``）。"""
    assert _scrub(["答案在此。\n", "<" + "tool_ca"])[0] == "答案在此。\n"
    assert _scrub(["答案在此。\n", "<" + "function=web_se"])[0] == "答案在此。\n"
    # 但“比大小”和 HTML 示例不是标记，不能顺手吃掉
    assert _scrub(["模板里写 <div>内容</div>即可\n"])[0] == "模板里写 <div>内容</div>即可\n"
    assert _scrub(["比大小 a", "[i]<n 常", "见\n"])[0] == "比大小 a[i]<n 常见\n"
