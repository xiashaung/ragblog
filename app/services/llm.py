"""LLM gateway — pluggable providers.

A small registry maps a provider name (``openai`` / ``deepseek`` / ``qwen`` /
``ollama`` / ``mlx`` / ``mock``) to a concrete ``LLMBackend``.

- OpenAI-compatible providers (openai / deepseek / qwen / ollama / mlx)
  share the same HTTP client — only ``api_key`` / ``base_url`` / model names
  differ. They are all wired through a single ``OpenAIBackend`` instance
  keyed by provider name.
- ``mock`` is a deterministic offline backend for dev / tests.

All backends implement the ``LLMBackend`` protocol and return the same
shapes used by the rest of the app (schemas in ``app/schemas/ai.py``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

from openai import AsyncOpenAI

from ..config import ProviderConfig, get_settings
from ..schemas.ai import (
    ChatChunk,
    ChatMessage,
    EmbeddingItem,
    EmbeddingRequest,
    EmbeddingResponse,
    Provider,
    ToolCall,
    ToolFunction,
    Usage,
)

# --- Provider → endpoint config ---


def _content_text(content: object) -> str:
    """Flatten a message ``content`` (str or multimodal parts) to plain text.

    Used by the offline mock backend and for rough token counting; image parts
    contribute a placeholder instead of their (huge) base64 payload.
    """
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content or []:  # type: ignore[union-attr]
        if isinstance(part, dict):
            ptype = part.get("type")
            if ptype == "text":
                parts.append(str(part.get("text", "")))
            elif ptype == "image_url":
                parts.append("[image]")
        else:
            ptype = getattr(part, "type", None)
            if ptype == "text":
                parts.append(str(getattr(part, "text", "")))
            elif ptype == "image_url":
                parts.append("[image]")
    return " ".join(parts)


def _provider_cfg(provider: Provider) -> ProviderConfig:
    settings = get_settings()
    return settings.provider_config(provider.value)


# --- Backend protocol ---


class LLMBackend(Protocol):
    """One LLM provider. Implementations must be async & no-arg-constructible."""

    provider: Provider

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> tuple[str, str | None, Usage, str]:
        """Return (content, finish_reason, usage, resolved_model)."""
        ...

    def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """异步生成器：逐块产出，不得把整段回复收集成 list 再返回。

        返回类型是 ``AsyncIterator`` 而不是 ``async def``：实现方是 async
        generator，收集完再一次性吐会把“流式”变成“等全部生成完再显示”。

        传了 ``tools`` 时，实现方必须把**分片回流**的 ``tool_calls`` 聚合完整后
        放在最后一块里给出（``ChatChunk.tool_calls``）：arguments 是一个字符一个字符
        流回来的，拼完整是 OpenAI 协议的细节，不该泄到调用层去处理。
        """
        ...

    async def embed(
        self, inputs: list[str], *, model: str | None = None
    ) -> tuple[list[EmbeddingItem], Usage, str]:
        ...

    async def embeddings(self, req: EmbeddingRequest) -> EmbeddingResponse:
        items, usage, model = await self.embed(req.input, model=req.model)
        return EmbeddingResponse(provider=self.provider, model=model, items=items, usage=usage)


# --- OpenAI-compatible (openai / deepseek / qwen / ollama / mlx) ---

# 思考内容的非标准字段名：各家兼容层没统一，DeepSeek / SiliconFlow 用
# ``reasoning_content``，oai-community 与部分网关用 ``reasoning``，Ollama 也叫
# ``thinking``。它们都不在 openai SDK 的 ``ChatCompletionChunk`` 类型里，
# 但 SDK 默认 extra="allow"，未声明字段会落进 ``model_extra``（属性访问只是
# 赌它恰好不与我们冲突），所以两条都读、以字典为准。
_REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")


def _delta_reasoning(delta: object) -> str:
    """从一个流式 delta 里取出思考增量（没有独立字段时返空串）。"""
    extra = getattr(delta, "model_extra", None)
    if isinstance(extra, dict):
        for key in _REASONING_KEYS:
            value = extra.get(key)
            if isinstance(value, str) and value:
                return value
    for key in _REASONING_KEYS:
        value = getattr(delta, key, None)
        if isinstance(value, str) and value:
            return value
    return ""


class OpenAIBackend:
    """Async OpenAI-compatible client for a single provider.

    For non-OpenAI providers the ``api_key`` may be empty — the OpenAI client
    still requires a non-empty string, so we substitute a placeholder.
    """

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        cfg = _provider_cfg(provider)
        self._cfg = cfg
        self._client = AsyncOpenAI(
            api_key=self._auth_key(provider, cfg),
            base_url=cfg.base_url,
        )

    @staticmethod
    def _auth_key(provider: Provider, cfg: ProviderConfig) -> str:
        """The OpenAI SDK requires a non-empty key string; a sentinel
        (``ollama`` / ``mlx``) is enough for local servers that ignore
        the Authorization header. Real providers (openai / deepseek /
        qwen) expect the user's key verbatim.
        """
        if cfg.api_key:
            return cfg.api_key
        if provider == Provider.OLLAMA:
            return "ollama"
        if provider == Provider.MLX:
            return "mlx"
        return "not-set"  # will fail auth at request time; the error surfaces clearly

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> tuple[str, str | None, Usage, str]:
        cfg = self._cfg
        kwargs: dict[str, Any] = dict(
            model=model or cfg.chat_model,
            messages=[m.model_dump(mode="json", exclude_none=True) for m in messages],
            temperature=temperature,
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if response_format is not None:
            kwargs["response_format"] = response_format

        resp = await self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        usage = Usage(
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
            total_tokens=resp.usage.total_tokens if resp.usage else 0,
        )
        return msg.content or "", resp.choices[0].finish_reason, usage, resp.model

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """Stream the reply as NDJSON-ready chunks (usage arrives on the last one).

        工具调用也落在最后一块：``tool_calls`` 聚合完成后随收尾 chunk 给出，
        让上层能靠它判断“模型这一轮其实是要调工具”，而不必自己拼接增量。

        思考内容走 ``ChatChunk.reasoning``（独立字段）；端点没开思考分路、把
        ``<think>`` 写进正文时，由上层的 ``reasoning.ThinkSplitter`` 拆，不在这里做。
        """
        cfg = self._cfg
        kwargs: dict[str, Any] = dict(
            model=model or cfg.chat_model,
            messages=[m.model_dump(mode="json", exclude_none=True) for m in messages],
            temperature=temperature,
            stream=True,
        )
        # Some gateways (ollama / mlx) do not support stream_options.
        try:
            kwargs["stream_options"] = {"include_usage": True}
        except TypeError:  # pragma: no cover
            pass
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if response_format is not None:
            kwargs["response_format"] = response_format
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        usage: Usage | None = None
        finish_reason: str | None = None
        # 按 choice 里的 index 聚合：并行多个工具调用时增量是交错回流的
        slots: dict[int, dict[str, str]] = {}

        # 逐块 yield：首字一到就推给客户端，不等整段生成完
        async for event in await self._client.chat.completions.create(**kwargs):
            if getattr(event, "usage", None) is not None:
                usage = Usage(
                    prompt_tokens=event.usage.prompt_tokens or 0,
                    completion_tokens=event.usage.completion_tokens or 0,
                    total_tokens=event.usage.total_tokens or 0,
                )
            for choice in event.choices or []:
                delta = choice.delta
                if delta is not None:
                    thinking = _delta_reasoning(delta)
                    if thinking:
                        # 字段路优先于标签路：这一份已经是干净的思考内容
                        yield ChatChunk(reasoning=thinking)
                    if delta.content:
                        yield ChatChunk(content=delta.content)
                    for frag in delta.tool_calls or []:
                        slot = slots.setdefault(
                            frag.index or 0, {"id": "", "name": "", "arguments": ""}
                        )
                        if frag.id:
                            slot["id"] = frag.id
                        fn = getattr(frag, "function", None)
                        if fn is not None:
                            # name 与 arguments 都可能是分片，只能累加不能覆写
                            if fn.name:
                                slot["name"] += fn.name
                            if fn.arguments:
                                slot["arguments"] += fn.arguments
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

        calls = [
            ToolCall(
                id=slot["id"],
                function=ToolFunction(name=slot["name"], arguments=slot["arguments"]),
            )
            for _, slot in sorted(slots.items())
            if slot["name"]
        ]
        yield ChatChunk(
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=calls or None,
            done=True,
        )

    async def embed(
        self, inputs: list[str], *, model: str | None = None
    ) -> tuple[list[EmbeddingItem], Usage, str]:
        cfg = self._cfg
        resp = await self._client.embeddings.create(
            model=model or cfg.embedding_model, input=inputs
        )
        items = [EmbeddingItem(index=d.index, embedding=list(d.embedding)) for d in resp.data]
        usage = Usage(
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=0,
            total_tokens=resp.usage.total_tokens if resp.usage else 0,
        )
        return items, usage, resp.model

    async def embeddings(self, req: EmbeddingRequest) -> EmbeddingResponse:
        items, usage, model = await self.embed(req.input, model=req.model)
        return EmbeddingResponse(provider=self.provider, model=model, items=items, usage=usage)


# --- Mock backend (deterministic, offline) ---


class MockBackend:
    """Offline backend: echo-style replies & hash embeddings.

    Lets the whole stack (auth, quota, RAG, tests) run without any real key
    or network.
    """

    def __init__(self, provider: Provider = Provider.MOCK) -> None:
        self.provider = provider

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> tuple[str, str | None, Usage, str]:
        cfg = _provider_cfg(self.provider)
        model = model or cfg.chat_model
        last = _content_text(messages[-1].content)
        if response_format and response_format.get("type") == "json_object":
            content = json.dumps(
                {"summary": last[:80], "confidence": 0.9}, ensure_ascii=False
            )
        else:
            content = f"[{self.provider.value}:{model}] {last}"
        prompt = sum(len(_content_text(m.content)) for m in messages)
        usage = Usage(
            prompt_tokens=prompt,
            completion_tokens=len(content),
            total_tokens=prompt + len(content),
        )
        return content, "stop", usage, model

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> AsyncIterator[ChatChunk]:
        # 离线 mock 不实现工具（它答不上“最新”的东西，假装会调工具只会让测试失真）；
        # 接了参数只为保持协议一致，真要测工具循环请注入脚本化的假后端。
        content, _, usage, _ = await self.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        tokens = list(content)
        for i in range(0, len(tokens), 5):
            yield ChatChunk(content="".join(tokens[i : i + 5]))
        yield ChatChunk(done=True, usage=usage)

    async def embed(
        self, inputs: list[str], *, model: str | None = None
    ) -> tuple[list[EmbeddingItem], Usage, str]:
        cfg = _provider_cfg(self.provider)
        model = model or cfg.embedding_model

        def _vec(text: str) -> list[float]:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            # 维度对齐知识库列（APP_RAG_VECTOR_DIM），否则 Postgres 的定长
            # vector 列装不下。字节循环复用，向量仍只由文本决定（确定性）。
            dim = get_settings().rag_vector_dim
            base = [digest[i % len(digest)] / 255 for i in range(dim)]
            norm = sum(x * x for x in base) ** 0.5 or 1.0
            return [round(x / norm, 6) for x in base]

        items = [EmbeddingItem(index=i, embedding=_vec(t)) for i, t in enumerate(inputs)]
        prompt = sum(len(t) for t in inputs)
        usage = Usage(prompt_tokens=prompt, completion_tokens=0, total_tokens=prompt)
        return items, usage, model

    async def embeddings(self, req: EmbeddingRequest) -> EmbeddingResponse:
        items, usage, model = await self.embed(req.input, model=req.model)
        return EmbeddingResponse(provider=self.provider, model=model, items=items, usage=usage)


# --- Factory + registry ---


def make_backend(provider: Provider) -> LLMBackend:
    """Build a backend instance for the given provider."""
    if provider is Provider.MOCK:
        return MockBackend(Provider.MOCK)
    return OpenAIBackend(provider)


_registry: dict[Provider, LLMBackend] = {}


def get_llm(provider: Provider | str | None = None) -> LLMBackend:
    """Return the backend for ``provider`` (or the default).

    Backends are cached per provider for the lifetime of the process.
    Pass ``provider=None`` to use ``APP_LLM_DEFAULT_PROVIDER``.
    """
    name = (provider or get_settings().llm_default_provider or "openai").lower()
    try:
        p = Provider(name)
    except ValueError as exc:
        raise ValueError(
            f"Unknown LLM provider {name!r}. Supported: {[e.value for e in Provider]}"
        ) from exc
    backend = _registry.get(p)
    if backend is None:
        backend = make_backend(p)
        _registry[p] = backend
    return backend


def reset_registry() -> None:
    """Drop all cached backends (use in tests when settings change)."""
    _registry.clear()
