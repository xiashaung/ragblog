"""AI-related request/response schemas."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Provider(StrEnum):
    """LLM providers supported by the registry.

    - ``openai``   : OpenAI official (and any OpenAI-compatible gateway via env)
    - ``deepseek`` : DeepSeek official
    - ``qwen``     : Aliyun DashScope / Tongyi Qianwen (OpenAI-compatible)
    - ``ollama``   : local Ollama HTTP service
    - ``mlx``      : Apple Silicon local MLX (mlx-lm / fastembed)
    - ``mock``     : offline deterministic backend
    """

    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    OLLAMA = "ollama"
    MLX = "mlx"
    MOCK = "mock"


class TextContent(BaseModel):
    """A text part of a multimodal message."""

    type: Literal["text"] = "text"
    text: str


class ImageURL(BaseModel):
    """Image reference: an http(s) URL or an inline ``data:image/...;base64`` URL."""

    url: str = Field(..., min_length=1)
    detail: Literal["auto", "low", "high"] | None = None


class ImageContent(BaseModel):
    """An image part of a multimodal message (OpenAI ``image_url`` shape)."""

    type: Literal["image_url"] = "image_url"
    image_url: ImageURL


ContentPart = Annotated[TextContent | ImageContent, Field(discriminator="type")]


class ToolFunction(BaseModel):
    """模型请求调用的函数。``arguments`` 保留模型原样吐出的 JSON 字符串：
    回传下一轮时只有原样带回才符合 OpenAI 协议，重新序列化会因键序/空格差异被判为篡改。"""

    name: str
    arguments: str = ""


class ToolCall(BaseModel):
    id: str = ""
    type: Literal["function"] = "function"
    function: ToolFunction


class ChatMessage(BaseModel):
    role: str = Field(..., description="Message role: system / user / assistant / tool")
    # ``str`` for plain text; a list of parts for multimodal (text + images).
    # 助手“只调工具不说话”的那一轮 content 为 None，所以它不再是必填。
    content: str | list[ContentPart] | None = None
    # 工具回合的三个字段：assistant 带上要调什么，tool 回带上是回哪一次调用
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if v not in {"system", "user", "assistant", "tool"}:
            raise ValueError("role must be one of: system / user / assistant / tool")
        return v

    @model_validator(mode="after")
    def _need_payload(self) -> "ChatMessage":
        """不传 content 就必须是带工具调用的助手消息，否则等于发了条空消息。"""
        if self.content is None and not self.tool_calls:
            raise ValueError("content is required unless this message carries tool_calls")
        return self


class ChatRequest(BaseModel):
    provider: Provider | None = Field(
        default=None,
        description="LLM provider override; defaults to APP_LLM_PROVIDER",
    )
    model: str | None = Field(
        default=None, description="Model override; defaults to the configured chat model"
    )
    messages: list[ChatMessage] = Field(..., min_length=1)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = Field(default=False, description="Return an NDJSON stream of chunks")
    response_format: dict | None = Field(
        default=None,
        description=(
            'Set to {"type": "json_object"} to force JSON; optionally add '
            '"json_schema" for strict structured output.'
        ),
    )
    web_mode: Literal["auto", "off", "forced"] = Field(
        default="auto",
        description=(
            "联网工具开关：auto=模型自己判断要不要查（已实测本地端点会正确调用）；"
            "forced=首轮强制先查；off=不声明工具。"
            "后端 web_search_enabled=false 时三者等价于 off。"
        ),
    )


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class WebSource(BaseModel):
    """一条联网命中的链接：既进流式事件，也随回答入会话历史供回放。"""

    title: str = ""
    url: str = Field(..., min_length=1)
    snippet: str = ""


class ToolEvent(BaseModel):
    """一次工具调用的过程事件（流式透出给前端渲染“搜了什么 / 命中哪几条”）。

    分两个 phase：``call`` 在真正发请求前给（前端能立刻显示“正在查…”与关键词），
    ``result`` 在拿到结果后给（带链接列表或失败原因）。模型的原样参数只在 call 阶段出现，
    因为它是给工程师看的调试信息，不该回喂给模型也不该重复出现在 result 里。
    """

    phase: Literal["call", "result"]
    name: str
    round: int = 1
    arguments: str | None = None
    # 服务端已解析好的一句话摘要（“搜索：北京天气”），前端直接显示它，
    # 解析失败时再退回原始 arguments，不抛错——模型偶尔会吐半截 JSON。
    detail: str | None = None
    ok: bool = True
    error: str | None = None
    sources: list[WebSource] = Field(default_factory=list)


class ChatResponse(BaseModel):
    id: str
    provider: Provider
    model: str
    content: str
    # 深度思考模型的思考过程（已按端点能力自动识别）；模型不输出思考时为空串
    reasoning: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None
    web_sources: list[WebSource] = Field(default_factory=list)


class ChatChunk(BaseModel):
    """One streamed delta (NDJSON line)."""

    content: str = ""
    # 思考增量与正文同路到达，但必须分字段：前端要能单独渲染“思考中”区块，
    # 不能把它混进答案正文（混进去会把推理过程当成结论展示给用户）
    reasoning: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None
    done: bool = False
    # 后端→路由层的「本轮结束且模型要求调工具」信号，不直接给浏览器（路由层会翻译成 tool 事件）
    tool_calls: list[ToolCall] | None = None
    # 给前端的检索过程：搜了什么词 / 命中哪些链接 / 失败原因
    tool: ToolEvent | None = None
    error: str | None = Field(
        default=None,
        description=(
            "流一旦开始就无法再改 HTTP 状态码，上游报错只能作为最后一行 chunk 透出，"
            "由前端渲染成失败气泡而不是静默截断。"
        ),
    )


class EmbeddingRequest(BaseModel):
    provider: Provider | None = Field(
        default=None,
        description="Embedding provider override; defaults to APP_LLM_PROVIDER",
    )
    model: str | None = None
    input: list[str] = Field(..., min_length=1, max_length=2048)


class EmbeddingItem(BaseModel):
    index: int
    embedding: list[float]


class EmbeddingResponse(BaseModel):
    provider: Provider
    model: str
    items: list[EmbeddingItem]
    usage: Usage | None = None


# --- RAG / knowledge base ---


class KnowledgeIngestRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1)
    provider: Provider | None = Field(
        default=None,
        description="Embedding provider to use when chunking",
    )
    chunk_size: int | None = Field(
        default=None,
        ge=100,
        le=4000,
        description="Chars per chunk; defaults to the service chunk size",
    )
    metadata: dict | None = None


class KnowledgeIngestResponse(BaseModel):
    knowledge_id: str
    provider: Provider
    chunks: int
    chars: int = 0
    task_id: str | None = Field(
        default=None,
        description="异步任务 ID（异步入库时返回）",
    )


class KnowledgePair(BaseModel):
    """One FAQ entry: the question is what retrieval matches on."""

    question: str = Field(..., min_length=1, max_length=2000)
    answer: str = Field(..., min_length=1)


class KnowledgePairsIngestRequest(BaseModel):
    """Ingest a batch of Q&A pairs as a single document."""

    title: str = Field(..., min_length=1, max_length=200)
    pairs: list[KnowledgePair] = Field(..., min_length=1, max_length=2000)
    provider: Provider | None = Field(
        default=None, description="Defaults to APP_LLM_DEFAULT_PROVIDER"
    )
    chunk_size: int | None = Field(default=None, ge=100, le=4000)
    metadata: dict | None = None


class KnowledgeFetchRequest(BaseModel):
    """Fetch a web page server-side and ingest its main text."""

    url: str = Field(..., min_length=1, max_length=2000)
    title: str | None = Field(
        default=None,
        max_length=200,
        description="Override the page <title> used as document title",
    )
    provider: Provider | None = Field(
        default=None, description="Defaults to APP_LLM_DEFAULT_PROVIDER"
    )
    chunk_size: int | None = Field(default=None, ge=100, le=4000)
    max_chars: int | None = Field(
        default=200_000, ge=1000, le=1_000_000, description="Truncate long pages"
    )


class KnowledgeFetchResponse(KnowledgeIngestResponse):
    title: str = ""
    url: str = ""
    final_url: str | None = None
    page_chars: int = 0
    truncated: bool = False


class KnowledgeDocItem(BaseModel):
    """One row of the management list (chunk bodies are not included)."""

    knowledge_id: str
    title: str
    provider: str
    chunks: int
    chars: int = 0
    preview: str = ""
    # False for documents ingested before the ``content`` column existed; they
    # can be searched but not edited / re-indexed until re-uploaded.
    has_content: bool = False
    source_file: str | None = None
    # How the document was ingested: file / text / faq / url.
    kind: str = "text"
    source_url: str | None = None
    metadata: dict | None = None
    ingested_at: float = 0.0
    updated_at: float = 0.0
    # True when the stored vectors were built by another provider than the
    # currently configured default (search will not reach them).
    stale: bool = False
    # 异步任务 ID（如果入队了）
    task_id: str | None = None


class KnowledgeListResponse(BaseModel):
    total: int
    offset: int = 0
    limit: int = 20
    default_provider: str
    items: list[KnowledgeDocItem]


class KnowledgeChunkItem(BaseModel):
    id: str
    index: int
    source: str
    text: str
    dim: int = 0


class KnowledgeDetailResponse(BaseModel):
    doc: KnowledgeDocItem
    content: str | None = None
    chunks: list[KnowledgeChunkItem]
    chunk_total: int


class KnowledgeDocUpdateRequest(BaseModel):
    """Rename and/or replace the text of a document.

    Changing ``content`` re-chunks and re-embeds the document, so it costs the
    same as an ingest; a pure rename is cheap.
    """

    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1)

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("content cannot be blank")
        return v


class KnowledgeBatchDeleteRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=500)


class KnowledgeBatchDeleteResponse(BaseModel):
    deleted: int
    failed: list[str] = []


class KnowledgeReindexResponse(BaseModel):
    knowledge_id: str
    provider: str
    previous_provider: str | None = None
    chunks: int
    chars: int
    task_id: str | None = Field(
        default=None,
        description="异步任务 ID（如果入队了）",
    )


class KnowledgeReplaceVectorsRequest(BaseModel):
    """更换向量模型后重建索引的请求体。"""

    provider: Provider | None = Field(
        default=None, description="新的向量模型 provider"
    )


class KnowledgeProviderStat(BaseModel):
    provider: str
    docs: int
    chunks: int
    stale: bool = False


class KnowledgeStatsResponse(BaseModel):
    docs: int
    chunks: int
    chars: int
    default_provider: str
    embedding_model: str = ""
    # "pgvector" = 距离在库里算、有 HNSW 索引；"json" = SQLite 回退方案
    vector_backend: str = "json"
    vector_dim: int = 0
    # 分片存在但没有可用向量（迁移时维度不符等），重建索引前检索不到
    vectorless_chunks: int = 0
    stale_docs: int = 0
    missing_content_docs: int = 0
    latest_updated_at: float | None = None
    by_provider: list[KnowledgeProviderStat] = []


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=4, ge=1, le=20)
    provider: Provider | None = None


class Citation(BaseModel):
    index: int
    knowledge_id: str
    source: str
    score: float
    snippet: str
    # 图片文档 / 含内嵌图片的文档：原图 data URL，供前端「显示图」（描述已入正文，可被检索）。
    images: list[str] = Field(default_factory=list)


class KnowledgeSearchResponse(BaseModel):
    query: str
    provider: Provider
    items: list[Citation]


class KnowledgeAnswerRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=4, ge=1, le=20)
    provider: Provider | None = None
    # Optional images (http(s) URL or inline base64 data URL) attached to the
    # question for multimodal answering. Only used when the configured chat
    # model is vision-capable.
    images: list[str] | None = Field(default=None, max_length=8)
    stream: bool = Field(default=False, description="是否启用流式响应")


class KnowledgeAnswerResponse(BaseModel):
    query: str
    provider: Provider
    answer: str
    citations: list[Citation]
    usage: Usage | None = None


class KnowledgeAnswerChunk(BaseModel):
    """知识库问答的流式增量块（NDJSON）。"""

    answer: str = ""  # 回答增量
    citations: list[Citation] = Field(default_factory=list)  # 引用来源（只在最后一块出现）
    usage: Usage | None = None  # Token 用量（只在最后一块出现）
    error: str | None = None  # 错误信息（流一旦开始只能走 chunk）


# ---------------- AI 对话会话持久化（仅本人可见） ----------------


class ChatSessionCreate(BaseModel):
    title: str = Field(
        default="",
        max_length=200,
        description="留空则建「新对话」，首条提问入库后自动改用问题前若干字作标题",
    )


class ChatSessionUpdate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class ChatSessionOut(BaseModel):
    id: str
    title: str
    created_at: float
    updated_at: float
    message_count: int = 0


class ChatSessionListResponse(BaseModel):
    """包成对象才能走统一信封（裸数组会多一层 data 嵌套）。"""

    sessions: list[ChatSessionOut] = Field(default_factory=list)


class ChatMessageIn(BaseModel):
    """一条待入库的消息（一次问答完成后由前端整轮追加）。"""

    role: str
    content: str = Field(default="", max_length=200_000)
    # 图片 data URL；体积大，超阈时只丢图保留正文（见服务层）
    images: list[str] = Field(default_factory=list, max_length=8)
    usage: Usage | None = None
    # 本轮联网命中的链接（只存标题+URL+短摘要，不存工具回喂给模型的全文）
    web_sources: list[WebSource] = Field(default_factory=list, max_length=20)
    # 思考过程全文（体积远小于图片，整段回放也要看，所以不设丢弃阈值）
    thinking: str = Field(default="", max_length=100_000)

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        # 只存对话双方；system 提示词不由前端写入历史
        if v not in {"user", "assistant"}:
            raise ValueError("role must be one of: user / assistant")
        return v


class ChatMessageOut(BaseModel):
    id: str
    seq: int
    role: str
    content: str
    images: list[str] = Field(default_factory=list)
    usage: Usage | None = None
    # 因超尺寸而未入库的图片张数（>0 时前端要明说“图片未持久化”而不是默默缺图）
    images_dropped: int = 0
    # 回答时的联网引用（回放能重现“这一答案是查来的、来源是哪几条”）
    web_sources: list[WebSource] = Field(default_factory=list)
    # 当时的思考过程（回放可展开；模型未输出思考时为空串）
    thinking: str = ""
    created_at: float


class ChatMessageListResponse(BaseModel):
    messages: list[ChatMessageOut] = Field(default_factory=list)


class ChatAppendRequest(BaseModel):
    # 上限 20：一轮最多“用户 + 助手”两条，留出修正/重新生成的余量
    messages: list[ChatMessageIn] = Field(..., min_length=1, max_length=20)


class ChatAppendResponse(BaseModel):
    session: ChatSessionOut
    messages: list[ChatMessageOut] = Field(default_factory=list)
