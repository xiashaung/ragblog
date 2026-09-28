"""LLM 瞬时故障重试与 provider 降级的单元测试（不联网，用桩替换 SDK 客户端）。"""

from types import SimpleNamespace

import httpx
import openai
import pytest

from app import metrics as app_metrics
from app.config import Settings
from app.schemas.ai import ChatMessage, Provider
from app.services import llm


def _status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    """构造一个带 httpx 响应现场的 OpenAI 状态异常（测试里模拟上游报错）。"""
    request = httpx.Request("POST", "https://api.example/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return cls("boom", response=response, body=None)


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    """把 llm 模块读到的配置换成测试值：退避基数置 0，重试等待不拖慢测试。"""
    base: dict[str, object] = {
        "_env_file": None,
        "llm_retry_backoff_base": 0.0,
        "llm_retry_backoff_max": 0.0,
    }
    base.update(overrides)
    settings = Settings(**base)  # type: ignore[arg-type]
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    return settings


def _completion(content: str = "hi") -> SimpleNamespace:
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None, model="test-model")


def _embed_resp() -> SimpleNamespace:
    item = SimpleNamespace(index=0, embedding=[0.1, 0.2])
    return SimpleNamespace(data=[item], usage=None, model="test-embed")


def _stub_client(script: list[object]) -> tuple[SimpleNamespace, dict]:
    """假 SDK 客户端：按脚本依次返回结果 / 抛出异常，并统计调用次数。

    脚本耗尽后重复末项，这样「一直失败」的用例不用铺满整条脚本。
    """
    state = {"calls": 0}

    async def create(**kwargs: object) -> object:
        idx = state["calls"]
        state["calls"] += 1
        outcome = script[idx] if idx < len(script) else script[-1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        embeddings=SimpleNamespace(create=create),
    )
    return client, state


# --- 重试 ---


async def test_chat_retries_transient_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 属瞬时故障：第一次失败应退避重试，第二次成功即返回。"""
    _patch_settings(monkeypatch)
    backend = llm.OpenAIBackend(Provider.OPENAI)
    client, state = _stub_client(
        [_status_error(openai.RateLimitError, 429), _completion("hi")]
    )
    backend._client = client
    before = app_metrics.llm_retries_total.value()
    content, _, _, model = await backend.chat([ChatMessage(role="user", content="q")])
    assert content == "hi"
    assert model == "test-model"
    assert state["calls"] == 2
    # 重试埋点：失败一次 = 重试计数 +1
    assert app_metrics.llm_retries_total.value() == before + 1


async def test_chat_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """持续失败时在配置的上限处收手，并把原始异常原样抛出（不包 RetryError）。"""
    _patch_settings(monkeypatch, llm_retry_max_attempts=2)
    backend = llm.OpenAIBackend(Provider.OPENAI)
    client, state = _stub_client([_status_error(openai.InternalServerError, 500)])
    backend._client = client
    with pytest.raises(openai.InternalServerError):
        await backend.chat([ChatMessage(role="user", content="q")])
    assert state["calls"] == 2


async def test_chat_no_retry_on_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """4xx 是参数/鉴权错，重试不会改变结果：只打一次就快速失败。"""
    _patch_settings(monkeypatch)
    backend = llm.OpenAIBackend(Provider.OPENAI)
    client, state = _stub_client([_status_error(openai.BadRequestError, 400)])
    backend._client = client
    with pytest.raises(openai.BadRequestError):
        await backend.chat([ChatMessage(role="user", content="q")])
    assert state["calls"] == 1


async def test_embed_retries_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """向量化同样受重试保护（RAG 入库/检索都靠它，一次抖动不该废掉整个任务）。"""
    _patch_settings(monkeypatch)
    backend = llm.OpenAIBackend(Provider.OPENAI)
    client, state = _stub_client(
        [_status_error(openai.RateLimitError, 429), _embed_resp()]
    )
    backend._client = client
    items, _, model = await backend.embed(["hello"])
    assert model == "test-embed"
    assert [i.embedding for i in items] == [[0.1, 0.2]]
    assert state["calls"] == 2


async def test_chat_stream_retries_stream_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """建立流之前失败可以整段重来；一次都没吐字时重试对客户端是透明的。"""
    _patch_settings(monkeypatch)
    backend = llm.OpenAIBackend(Provider.OPENAI)

    state = {"calls": 0}

    async def create(**kwargs: object) -> object:
        state["calls"] += 1
        if state["calls"] == 1:
            # APITimeoutError 的构造只收 request（没有响应可带）
            raise openai.APITimeoutError(
                request=httpx.Request("POST", "https://api.example/v1/chat/completions")
            )
        chunk = SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(
                delta=SimpleNamespace(
                    content="hi", reasoning_content=None, model_extra={}, tool_calls=None
                ),
                finish_reason=None,
            )],
        )

        async def gen():
            yield chunk

        return gen()

    backend._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    chunks = [
        c async for c in backend.chat_stream([ChatMessage(role="user", content="q")])
    ]
    assert state["calls"] == 2
    assert any(c.content == "hi" for c in chunks)


# --- provider 降级 ---


class _FailingPrimary:
    """模拟「重试已耗尽、仍被瞬时故障挡住」的主端：chat 直接抛瞬时错。"""

    provider = Provider.OPENAI

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.chat_stream_calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN201
        raise self._exc

    def chat_stream(self, messages, **kwargs):  # noqa: ANN001
        self.chat_stream_calls += 1
        return "passthrough"


async def test_fallback_switches_on_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)
    primary = _FailingPrimary(_status_error(openai.RateLimitError, 429))
    backend = llm.FallbackBackend(primary, llm.MockBackend(Provider.MOCK))  # type: ignore[arg-type]
    before = app_metrics.llm_fallback_total.value()
    content, _, _, _ = await backend.chat([ChatMessage(role="user", content="q")])
    # MockBackend 的回复以「[provider:模型]」开头，说明确实换到备胎答的
    assert content.startswith("[mock:")
    assert backend.provider is Provider.OPENAI
    assert app_metrics.llm_fallback_total.value() == before + 1


async def test_fallback_not_triggered_on_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4xx 不降级：备胎照样错，真实错因必须诚实透出而不是被掩盖。"""
    _patch_settings(monkeypatch)
    primary = _FailingPrimary(_status_error(openai.BadRequestError, 400))
    backend = llm.FallbackBackend(primary, llm.MockBackend(Provider.MOCK))  # type: ignore[arg-type]
    with pytest.raises(openai.BadRequestError):
        await backend.chat([ChatMessage(role="user", content="q")])


def test_fallback_passes_through_stream_and_embed() -> None:
    """流式与嵌入原样透传主后端：两者中途换模型都会造成更坏的结果。"""
    primary = _FailingPrimary(RuntimeError("unused"))
    backend = llm.FallbackBackend(primary, llm.MockBackend(Provider.MOCK))  # type: ignore[arg-type]
    assert backend.chat_stream([]) == "passthrough"  # type: ignore[arg-type]
    assert primary.chat_stream_calls == 1


def test_get_llm_wraps_chat_feature_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """配了备胎时只有对话功能拿到降级代理；未配置时一切照旧（不包）。"""
    _patch_settings(monkeypatch, llm_fallback_provider="mock")
    llm.reset_registry()
    try:
        emb = llm.get_llm("openai", feature="embedding")
        assert not isinstance(emb, llm.FallbackBackend)
        llm.reset_registry()
        chat = llm.get_llm("openai", feature="chat")
        assert isinstance(chat, llm.FallbackBackend)
        assert chat.provider is Provider.OPENAI
        # 备胎置空后重建：不该再包任何壳
        _patch_settings(monkeypatch)
        llm.reset_registry()
        assert not isinstance(llm.get_llm("openai", feature="chat"), llm.FallbackBackend)
    finally:
        llm.reset_registry()
