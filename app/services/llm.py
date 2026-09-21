"""LLM 网关：可插拔的 provider 注册表，上层只对着协议写。

一个小型注册表把 provider 名字（``openai`` / ``deepseek`` / ``qwen`` / ``ollama`` /
``mlx`` / 自定义 / ``mock``）映到一个 ``LLMBackend`` 实例上：

- OpenAI 兼容系（openai / deepseek / qwen / ollama / mlx 与自定义端点）共用同一个
  HTTP 客户端，只有 ``api_key`` / ``base_url`` / 模型名不同，所以都是同一个
  ``OpenAIBackend`` 类，按 provider 名字各存一个实例。
- ``mock`` 是确定性的离线后端，给开发与测试用（不需要任何真 Key）。

取配置的口径：端点与模型名一律走 :func:`_provider_cfg`（「模型管理」里的
数据库配置优先、``.env`` 兜底），所以后台改完下一次请求就生效。
所有后端返回的形状统一由 ``app/schemas/ai.py`` 约束，调用方不需知道底下是谁。
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
from . import provider_store

# --- provider → 端点配置 ---


def _content_text(content: object) -> str:
    """把一条消息的 ``content``（纯文本或多模态 parts）展平成纯文本。

    给离线 mock 后端和粗略 token 统计用；图片部分只计一个占位串，
    不然会把 base64 本体算进长度（动辄几十万字符）。
    兼容 dict 与属性对象两种形态，因为上游 schemas 与 OpenAI SDK 各给一种。
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


def _provider_cfg(provider: Provider | str) -> ProviderConfig:
    """取 provider 的调用参数：「模型管理」里的数据库配置优先，.env 兜底。

    缓存由 ``services.provider_store`` 维护（启动时加载、后台每次写入后刷新），
    所以这里保持同步：改了界面里的 base_url / api_key / 模型名，下一次请求就用新的。
    """
    name = str(getattr(provider, "value", provider)).strip().lower()
    cfg = provider_store.get_provider_config(name)
    if cfg is not None:
        return cfg
    return get_settings().provider_config(name)


# --- Backend protocol ---


class LLMBackend(Protocol):
    """一个 LLM provider 的能力边界（对话 + 嵌入）。

    实现方必须是异步、可不传参构造；``chat_stream`` 的签名是「非 async 的异步
    生成器」（返 ``AsyncIterator``），写成 ``async def`` 返 list 就不是流式了。
    实例由 :func:`make_backend` 造，调用方一律走 :func:`get_llm`。
    """

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
        """一次性对话：返 ``(正文, finish_reason, usage, 实际模型名)``。

        第 4 项用服务端回的值而不是请求里的名字，展示时才不会把「问的模型」
        当成「答的模型」（网关有时会路由到别的副本）。
        """
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
        generator，收集完再一次性吐会把「流式」变成「等全部生成完再显示」。

        传了 ``tools`` 时，实现方必须把**分片回流**的 ``tool_calls`` 聚合完整后
        放在最后一块里给出（``ChatChunk.tool_calls``）：arguments 是一个字符一个字符
        流回来的，拼完整是 OpenAI 协议的细节，不该泄到调用层去处理。
        """
        ...

    async def embed(
        self, inputs: list[str], *, model: str | None = None
    ) -> tuple[list[EmbeddingItem], Usage, str]:
        """批量向量化，返 ``(向量列表, usage, 实际模型名)``。

        录入与检索共用这一个入口：两边必须用同一个模型，否则存与查的向量
        空间互不认（分片行会记下录入时的 provider）。
        """
        ...

    async def embeddings(self, req: EmbeddingRequest) -> EmbeddingResponse:
        """协议里的默认实现：把 :meth:`embed` 的结果拼成对外的响应体。

        各后端直接沿用这段即可，不必重复写；要改返回形状应改 schema，
        而不是在某个后端里单独覆盖。
        """
        items, usage, model = await self.embed(req.input, model=req.model)
        return EmbeddingResponse(provider=self.provider, model=model, items=items, usage=usage)


# --- OpenAI 兼容系（openai / deepseek / qwen / ollama / mlx / 自定义端点）---

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
    """单个 provider 的 OpenAI 兼容异步客户端。

    非 OpenAI 的 provider 允许 ``api_key`` 为空，但 OpenAI SDK 要求它是非空串，
    所以由 :meth:`_auth_key` 补占位值。
    实例本身只携带「连到哪」（base_url/Key），而「用哪个模型」每次请求重读配置。
    """

    def __init__(self, provider: Provider) -> None:
        """按当时的配置建客户端；后续变更由 :meth:`_current` 接手。"""
        self.provider = provider
        self._cfg = _provider_cfg(provider)
        self._client = AsyncOpenAI(
            api_key=self._auth_key(provider, self._cfg),
            base_url=self._cfg.base_url,
        )

    def _current(self) -> ProviderConfig:
        """每次请求重新取配置：模型名在后台改了不必重建客户端。

        base_url / api_key 变了则 OpenAI 客户端必须重建（它们写死在实例里），
        所以比对签名后按需换。provider_store 刷新时也会清空整个后端缓存，
        这里是两道防线里更细的那一道。
        """
        cfg = _provider_cfg(self.provider)
        if (cfg.base_url, cfg.api_key) != (self._cfg.base_url, self._cfg.api_key):
            self._client = AsyncOpenAI(
                api_key=self._auth_key(self.provider, cfg), base_url=cfg.base_url
            )
        self._cfg = cfg
        return cfg

    @staticmethod
    def _auth_key(provider: Provider, cfg: ProviderConfig) -> str:
        """给 SDK 凑一个可用的 Key 串。

        OpenAI SDK 要求 Key 非空；本地服务（``ollama`` / ``mlx``）忽略
        Authorization 头，给个哨兵串就行。真正的线上 provider（openai /
        deepseek / qwen）必须原样用用户配的 Key；都没配时给 ``not-set``，
        让它在请求时报错——错得清楚胜过启动时就炸。
        """
        if cfg.api_key:
            return cfg.api_key
        if provider == Provider.OLLAMA:
            return "ollama"
        if provider == Provider.MLX:
            return "mlx"
        return "not-set"  # 到请求时才报鉴权错，错因能清楚透出

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> tuple[str, str | None, Usage, str]:
        """非流式对话：直接拿完整回复。

        ``model`` 不传时用配置里的 ``chat_model``；``response_format`` 用于让端点
        返 JSON（智能问数、结论生成靠它）。
        """
        cfg = self._current()
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
        """把回复按块流出（NDJSON 可直接用；usage 落在最后一块）。

        工具调用也落在最后一块：``tool_calls`` 聚合完成后随收尾 chunk 给出，
        让上层能靠它判断「模型这一轮其实是要调工具」，而不必自己拼接增量。

        思考内容走 ``ChatChunk.reasoning``（独立字段）；端点没开思考分路、把
        ``<think>`` 写进正文时，由上层的 ``reasoning.ThinkSplitter`` 拆，不在这里做。
        """
        cfg = self._current()
        kwargs: dict[str, Any] = dict(
            model=model or cfg.chat_model,
            messages=[m.model_dump(mode="json", exclude_none=True) for m in messages],
            temperature=temperature,
            stream=True,
        )
        # 部分网关（ollama / mlx）不支持 stream_options，带了会报错。
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
        """走 OpenAI 兼容的 ``/embeddings`` 接口，把 ``input`` 批量转向量。

        返回项里带 ``index``，上层按它对齐输入顺序（SDK 不保证回得有序）。
        """
        cfg = self._current()
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
        """沿用协议里的默认实现（把 :meth:`embed` 包成响应体）。"""
        items, usage, model = await self.embed(req.input, model=req.model)
        return EmbeddingResponse(provider=self.provider, model=model, items=items, usage=usage)


# --- Mock 后端（确定性、离线）---


class MockBackend:
    """离线后端：回音式回复 + 由哈希派生的向量。

    让整条链路（鉴权、额度、RAG、测试）不依赖任何真 Key 与网络；回复只
    取决于输入文本，所以同样的用例永远给出同样的结果。注意它不联网，
    因此答案内容没有任何意义，只能用来验证「链路通不通」。
    """

    def __init__(self, provider: Provider = Provider.MOCK) -> None:
        """默认就是 mock；也可拿它假扮别的 provider 名字（测试里自定义 provider 用）。"""
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
        """回音回复：把最后一条用户消息原样贴回来。

        要 JSON 时（``response_format=json_object``）给一个形似结论的固定结构，
        让靠 JSON 输出的链路（如智能问数）在测试里也能解析成功。
        usage 用字符数当作 token 数估算，只求稳确定性，不代表真实计费。
        """
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
        """把同一份回复按 5 个字符切块流出，让前端能验到流式行为。"""
        # 离线 mock 不实现工具（它答不上「最新」的东西，假装会调工具只会让测试失真）；
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
        """用 sha256 派生「向量」：不需模型也不联网，但保得住确定性。"""
        cfg = _provider_cfg(self.provider)
        model = model or cfg.embedding_model

        def _vec(text: str) -> list[float]:
            """单个文本 -> 单位长度向量（同一输入永远同一向量）。"""
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
        """沿用协议里的默认实现。"""
        items, usage, model = await self.embed(req.input, model=req.model)
        return EmbeddingResponse(provider=self.provider, model=model, items=items, usage=usage)


# --- 工厂 + 缓存注册表 ---


def make_backend(provider: Provider) -> LLMBackend:
    """为指定 provider 新建一个后端实例（mock 走离线后端，其余都是 OpenAI 兼容）。"""
    if provider is Provider.MOCK:
        return MockBackend(Provider.MOCK)
    return OpenAIBackend(provider)


_registry: dict[str, LLMBackend] = {}


def default_provider_for(feature: str = "chat") -> str:
    """某功能在「系统配置」里选定的 provider；没配则回落 ``APP_LLM_DEFAULT_PROVIDER``。

    feature 只影响「默认选哪个 provider」，与 ``.env`` 里一个全局默认值包办一切相区别。
    """
    return (
        provider_store.get_feature_provider(feature)
        or get_settings().llm_default_provider
        or "openai"
    )


def default_chat_provider() -> str:
    """对话默认 provider（= 系统配置里的 chat_provider）。"""
    return default_provider_for("chat")


def get_llm(
    provider: Provider | str | None = None, *, feature: str = "chat"
) -> LLMBackend:
    """按 ``provider`` 取后端实例（带缓存，同一 provider 复用同一个）。

    不传 ``provider`` 时用「系统配置」里该功能选定的 provider（而不是 .env），
    这样后台换了模型下一次请求就生效；显式传了则以请求为准。
    已知 provider = 枚举内置项 + 数据库里配过的自定义项。
    """
    if provider is not None:
        name = str(getattr(provider, "value", provider)).strip().lower()
    else:
        name = default_provider_for(feature).strip().lower()
    known = {p.value for p in Provider} | set(provider_store.known_providers())
    if name not in known:
        raise ValueError(
            f"Unknown LLM provider {name!r}. Supported: {sorted(known)}"
        )
    backend = _registry.get(name)
    if backend is None:
        backend = make_backend(Provider(name))
        _registry[name] = backend
    return backend


def resolve_name(provider: Provider | str | None = None, *, feature: str = "chat") -> str:
    """``get_llm`` 会实际用哪个 provider（只要名字，不构造后端）。"""
    if provider is not None:
        return str(getattr(provider, "value", provider)).strip().lower()
    return default_provider_for(feature).strip().lower()


def chat_model(provider: Provider | str | None = None, *, feature: str = "chat") -> str:
    """该 provider 在「模型管理」里配的聊天模型名（回落 .env）。"""
    return _provider_cfg(resolve_name(provider, feature=feature)).chat_model


def embedding_model(provider: Provider | str | None = None, *, feature: str = "embedding") -> str:
    """该 provider 在「模型管理」里配的向量模型名（回落 .env）。

    与 ``chat_model`` 对称：展示用的模型名必须和实际调用取的是同一份配置，
    否则「概览」页会报一个 .env 里早已换掉的名字。
    """
    return _provider_cfg(resolve_name(provider, feature=feature)).embedding_model


def reset_registry() -> None:
    """清空后端缓存（测试里改了配置时用）。

    生产上不用手动调：``provider_store`` 刷新模型管理配置时会清一次，
    使得 base_url / Key 的变更能立刻反映到新实例上。
    """
    _registry.clear()
