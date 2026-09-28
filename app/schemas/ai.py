"""AI 相关接口的请求 / 响应模型。

分三块：通用对话（``ChatRequest`` / ``ChatResponse`` / ``ChatChunk``）、向量化
（``EmbeddingRequest`` 系列）、知识库 RAG（``Knowledge*``）与对话会话持久化（``ChatSession*``）。
流式接口一律走 NDJSON，每行一个 ``*Chunk`` 对象。
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..config import custom_provider_member


class Provider(StrEnum):
    """模型注册表支持的 LLM 后端。

    - ``openai``   : OpenAI 官方（经 env 配置也可指任何 OpenAI 兼容网关）
    - ``deepseek`` : DeepSeek 官方
    - ``qwen``     : 阿里云 DashScope / 通义千问（OpenAI 兼容）
    - ``ollama``   : 本地 Ollama HTTP 服务
    - ``mlx``      : Apple Silicon 本地 MLX（mlx-lm / fastembed）
    - ``mock``     : 离线确定性假后端

    「模型接入」里新建的自定义 provider 由 ``_missing_`` 动态支持（白名单由
    ``services.provider_store`` 刷新缓存时登记），没配过的名字依旧校验不通过。
    """

    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    OLLAMA = "ollama"
    MLX = "mlx"
    MOCK = "mock"

    @classmethod
    def _missing_(cls, value):
        """枚举未命中时查自定义 provider 白名单，命中则动态补出一个成员。"""
        return custom_provider_member(cls, value)


class TextContent(BaseModel):
    """多模态消息里的一个文本片段。"""

    type: Literal["text"] = "text"
    text: str


class ImageURL(BaseModel):
    """图片引用：http(s) 链接，或内联的 ``data:image/...;base64`` 数据 URL。"""

    url: str = Field(..., min_length=1)
    detail: Literal["auto", "low", "high"] | None = None


class ImageContent(BaseModel):
    """多模态消息里的一个图片片段（按 OpenAI 的 ``image_url`` 形状）。"""

    type: Literal["image_url"] = "image_url"
    image_url: ImageURL


ContentPart = Annotated[TextContent | ImageContent, Field(discriminator="type")]


class ToolFunction(BaseModel):
    """模型请求调用的函数。``arguments`` 保留模型原样吐出的 JSON 字符串：
    回传下一轮时只有原样带回才符合 OpenAI 协议，重新序列化会因键序/空格差异被判为篡改。"""

    name: str
    arguments: str = ""


class ToolCall(BaseModel):
    """模型要求调用工具的一条声明（包一层 ``function`` 以对齐 OpenAI 报文）。"""

    id: str = ""
    type: Literal["function"] = "function"
    function: ToolFunction


class ChatMessage(BaseModel):
    """一条对话消息；回喂模型时四种角色共用同一形状。"""

    role: str = Field(..., description="消息角色：system / user / assistant / tool")
    # 纯文本时是 ``str``；多模态（文本 + 图片）时是片段列表。
    # 助手「只调工具不说话」的那一轮 content 为 None，所以它不再是必填。
    content: str | list[ContentPart] | None = None
    # 工具回合的三个字段：assistant 带上要调什么，tool 回带上是回哪一次调用
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        """角色只能是指定的四种（拼写错不如直接 422）。"""
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
    """通用对话请求：不传 provider/model 就走环境里配的默认对话模型。"""

    provider: Provider | None = Field(
        default=None,
        description="覆盖用的 LLM 后端；不传则用 APP_LLM_PROVIDER",
    )
    model: str | None = Field(
        default=None, description="覆盖用的模型名；不传则用已配置的默认对话模型"
    )
    messages: list[ChatMessage] = Field(..., min_length=1)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = Field(default=False, description="为真则返回 NDJSON 分块流")
    response_format: dict | None = Field(
        default=None,
        description=(
            '传 {"type": "json_object"} 可强制输出 JSON；还可加 '
            '"json_schema" 拿到严格的结构化输出。'
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
    """Token 用量；上游不返回时各项保持 0，不当作错误。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class WebSource(BaseModel):
    """一条联网命中的链接：既进流式事件，也随回答入会话历史供回放。"""

    title: str = ""
    url: str = Field(..., min_length=1)
    snippet: str = ""


class ToolEvent(BaseModel):
    """一次工具调用的过程事件（流式透出给前端渲染「搜了什么 / 命中哪几条」）。

    分两个 phase：``call`` 在真正发请求前给（前端能立刻显示「正在查…」与关键词），
    ``result`` 在拿到结果后给（带链接列表或失败原因）。模型的原样参数只在 call 阶段出现，
    因为它是给工程师看的调试信息，不该回喂给模型也不该重复出现在 result 里。
    """

    phase: Literal["call", "result"]
    name: str
    round: int = 1
    arguments: str | None = None
    # 服务端已解析好的一句话摘要（「搜索：北京天气」），前端直接显示它，
    # 解析失败时再退回原始 arguments，不抛错——模型偶尔会吐半截 JSON。
    detail: str | None = None
    ok: bool = True
    error: str | None = None
    sources: list[WebSource] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """非流式对话的完整响应（一次性返整段正文）。"""

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
    """一个流式增量（对应 NDJSON 的一行）。"""

    content: str = ""
    # 思考增量与正文同路到达，但必须分字段：前端要能单独渲染「思考中」区块，
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
    """向量化请求：``input`` 是一段文本列表，逐段返一条向量。"""

    provider: Provider | None = Field(
        default=None,
        description="覆盖用的向量模型后端；不传则用 APP_LLM_PROVIDER",
    )
    model: str | None = None
    input: list[str] = Field(..., min_length=1, max_length=2048)


class EmbeddingItem(BaseModel):
    """一条向量，``index`` 是它对应 ``input`` 里的第几段。"""

    index: int
    embedding: list[float]


class EmbeddingResponse(BaseModel):
    """向量化响应：回实际用的后端与模型名，方便确认维度对不对。"""

    provider: Provider
    model: str
    items: list[EmbeddingItem]
    usage: Usage | None = None


# --- RAG / 知识库 ---


#: 录入正文的格式。三者都只出现在**请求**里：``html`` 会在服务层被压成 Markdown
#: （见 :mod:`app.services.markdown_text`），所以落库与回显的只有 ``text`` / ``markdown``。
#: 保留 ``text`` 这个默认值是为了老前端与历史数据：不传就等于「按纯文本处理」。
ContentFormat = Literal["text", "markdown", "html"]


class DocStatus(StrEnum):
    """知识库文档的向量化状态机（与 ``KnowledgeDoc.status`` 取值一致）。

    - ``indexed``     已学习：向量与当前嵌入模型一致，可被检索；
    - ``pending``     待重建：需重建（新建尚未学习 / 切换嵌入模型后），检索排除；
    - ``processing``  处理中：后台 worker 正在重新向量化，检索排除；
    - ``failed``      重建失败：上次重建报错（维度不符 / 后端失败 / 无原文），可重试。
    """

    INDEXED = "indexed"
    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"


class KnowledgeIngestRequest(BaseModel):
    """文本录入：一段正文喂给服务层，由它切片 + 向量化。"""

    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1)
    format: ContentFormat = Field(
        default="text",
        description="正文格式：text / markdown 原样入库，html 先转成 Markdown 再入库",
    )
    provider: Provider | None = Field(
        default=None,
        description="切片向量化时用哪个向量后端",
    )
    chunk_size: int | None = Field(
        default=None,
        ge=100,
        le=4000,
        description="每片多少字；不传则用服务层的默认分片长度",
    )
    metadata: dict | None = None


class KnowledgeIngestResponse(BaseModel):
    """录入结果：文档 ID + 切了多少片。"""

    knowledge_id: str
    status: DocStatus = DocStatus.INDEXED
    chunks: int
    chars: int = 0
    task_id: str | None = Field(
        default=None,
        description="异步任务 ID（异步入库时返回）",
    )


class KnowledgePair(BaseModel):
    """一条 FAQ：检索时只拿「问」去匹配，「答」随引用回给模型。"""

    question: str = Field(..., min_length=1, max_length=2000)
    answer: str = Field(..., min_length=1)


class KnowledgePairsIngestRequest(BaseModel):
    """把一批问答对当作一份文档录入。"""

    title: str = Field(..., min_length=1, max_length=200)
    pairs: list[KnowledgePair] = Field(..., min_length=1, max_length=2000)
    provider: Provider | None = Field(
        default=None, description="不传则用 APP_LLM_DEFAULT_PROVIDER"
    )
    chunk_size: int | None = Field(default=None, ge=100, le=4000)
    metadata: dict | None = None


class KnowledgeFetchRequest(BaseModel):
    """服务端抓一个网页，只取正文入库。"""

    url: str = Field(..., min_length=1, max_length=2000)
    title: str | None = Field(
        default=None,
        max_length=200,
        description="覆盖页面 ``<title>``，指定用作文档标题",
    )
    provider: Provider | None = Field(
        default=None, description="不传则用 APP_LLM_DEFAULT_PROVIDER"
    )
    chunk_size: int | None = Field(default=None, ge=100, le=4000)
    max_chars: int | None = Field(
        default=200_000, ge=1000, le=1_000_000, description="超长页面削到这个长度"
    )


class KnowledgeFetchResponse(KnowledgeIngestResponse):
    """抓取入库结果：在录入结果上补上页面自身的信息。"""

    title: str = ""
    url: str = ""
    final_url: str | None = None
    page_chars: int = 0
    truncated: bool = False


class KnowledgeDocItem(BaseModel):
    """管理列表里的一行（不含分片正文）。"""

    knowledge_id: str
    title: str
    # 向量化状态（indexed/pending/processing/failed）：决定能否被检索与是否需重建。
    status: DocStatus = DocStatus.PENDING
    # 上次重建失败的原因（仅 status=failed 时有值），供前端 tooltip 展示。
    reindex_error: str | None = None
    chunks: int
    chars: int = 0
    preview: str = ""
    # 在 ``content`` 列存在之前录入的老文档为 False：它们能搜，
    # 但要重新上传后才能编辑 / 重建索引。
    has_content: bool = False
    source_file: str | None = None
    # 入库途径：file / text / faq / url。
    kind: str = "text"
    # 正文格式（落库只有 text / markdown）：markdown 的文档前端才值得按语法渲染。
    format: str = "text"
    source_url: str | None = None
    metadata: dict | None = None
    ingested_at: float = 0.0
    updated_at: float = 0.0
    # 异步任务 ID（如果入队了）
    task_id: str | None = None


class KnowledgeListResponse(BaseModel):
    """分页的文档列表，带上当前默认向量后端供前端标「陈旧」。"""

    total: int
    offset: int = 0
    limit: int = 20
    default_provider: str
    items: list[KnowledgeDocItem]


class KnowledgeChunkItem(BaseModel):
    """详情页里的一片：带正文与维度，用于展示向量到底存没存上。"""

    id: str
    index: int
    source: str
    text: str
    dim: int = 0


class KnowledgeDetailResponse(BaseModel):
    """单文档详情：元信息 + 全文 + 分片清单。"""

    doc: KnowledgeDocItem
    content: str | None = None
    chunks: list[KnowledgeChunkItem]
    chunk_total: int


class KnowledgeDocUpdateRequest(BaseModel):
    """改名和/或替换正文。

    改 ``content`` 会重新切片并重新向量化，代价等价于一次录入；只改名则很便宜。
    """

    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1)
    format: ContentFormat | None = Field(
        default=None,
        description="content 的格式；不传则沿用文档原有格式（只改名时无需关心）",
    )

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str | None) -> str | None:
        """允许不传正文，但传了就不能是空串 / 全空白（否则等于把正文清空）。"""
        if v is not None and not v.strip():
            raise ValueError("content cannot be blank")
        return v


class KnowledgeBatchDeleteRequest(BaseModel):
    """批量删文档：一次最多 500 个 ID。"""

    ids: list[str] = Field(..., min_length=1, max_length=500)


class KnowledgeBatchDeleteResponse(BaseModel):
    """批量删结果：``failed`` 里是没删掉的 ID（不存在或已释放）。"""

    deleted: int
    failed: list[str] = []


class KnowledgeReindexResponse(BaseModel):
    """重建索引的结果：回当时状态与新分片数。"""

    knowledge_id: str
    status: DocStatus
    chunks: int
    chars: int
    task_id: str | None = Field(
        default=None,
        description="异步任务 ID（如果入队了）",
    )


class KnowledgeReindexAllResponse(BaseModel):
    """全库强制重建：回本次圈定的文档数与（异步入队时的）任务 ID。"""

    enqueued: int
    task_id: str | None = Field(
        default=None,
        description="REINDEX_ALL 后台任务 ID；同步模式下为 None",
    )


class RagTaskStatus(BaseModel):
    """一个 RAG 后台任务的状态快照（录入后拿 task_id 轮询这个）。

    ``status`` 取值：pending / running / success / failed。未完成任务
    ``result``/``error`` 为空；失败时 ``error`` 带异常摘要。
    """

    task_id: str
    task_type: str
    status: str
    knowledge_id: str | None = None
    result: dict | None = None
    error: str | None = None
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None


class RagTaskListResponse(BaseModel):
    """分页的任务列表（管理页展示最近任务）。"""

    total: int
    offset: int = 0
    limit: int = 20
    items: list[RagTaskStatus]


class KnowledgeStatusStat(BaseModel):
    """按向量化状态分组的统计。"""

    status: str
    docs: int
    chunks: int


class KnowledgeStatsResponse(BaseModel):
    """知识库总体概况（概览页、健康判断与重录入提示都靠这个）。"""

    docs: int
    chunks: int
    chars: int
    # 当前配置的嵌入 provider（仅作展示：现在整个库只对应这一个模型）
    default_provider: str
    embedding_model: str = ""
    # "pgvector" = 距离在库里算、有 HNSW 索引；"json" = SQLite 回退方案
    vector_backend: str = "json"
    vector_dim: int = 0
    # 分片存在但没有可用向量（迁移时维度不符等），重建索引前检索不到
    vectorless_chunks: int = 0
    # 四态计数：待重建 / 处理中 / 已学习 / 重建失败
    pending_docs: int = 0
    processing_docs: int = 0
    indexed_docs: int = 0
    failed_docs: int = 0
    missing_content_docs: int = 0
    latest_updated_at: float | None = None
    by_status: list[KnowledgeStatusStat] = []


class KnowledgeSearchRequest(BaseModel):
    """裸检索：只要 top-k 引用，不叫模型组织回答。"""

    query: str = Field(..., min_length=1)
    top_k: int = Field(default=4, ge=1, le=20)
    provider: Provider | None = None


class Citation(BaseModel):
    """一条引用：``index`` 是正文里角标序号，``snippet`` 是命中的分片原文。"""

    index: int
    knowledge_id: str
    source: str
    score: float
    snippet: str
    # 图片文档 / 含内嵌图片的文档：原图 data URL，供前端「显示图」（描述已入正文，可被检索）。
    images: list[str] = Field(default_factory=list)


class KnowledgeSearchResponse(BaseModel):
    """检索响应：回实际用的后端，方便比对「是不是向量的那个后端」。"""

    query: str
    provider: Provider
    items: list[Citation]


class KnowledgeImageSearchRequest(BaseModel):
    """文搜图：拿一段文字去召回最相近的图片（跨模态）。"""

    query: str = Field(..., min_length=1)
    top_k: int = Field(default=8, ge=1, le=50)
    provider: Provider | None = None


class ImageHit(BaseModel):
    """一条图片命中：落盘地址 + 归属文档 + 相似度分数。"""

    knowledge_id: str
    image_url: str
    title: str
    score: float


class KnowledgeImageSearchResponse(BaseModel):
    """文搜图响应：回实际用的 VL provider 与命中列表。"""

    query: str
    provider: Provider
    items: list[ImageHit]


class KnowledgeAnswerRequest(BaseModel):
    """知识库问答：先检索再组提示词交给模型，最后带引用回答。"""

    query: str = Field(..., min_length=1)
    top_k: int = Field(default=4, ge=1, le=20)
    provider: Provider | None = None
    # 附在问题上的图片（http(s) 链接或内联 base64 数据 URL），供多模态回答；
    # 只有当配置的对话模型具备视觉能力时才会生效。
    images: list[str] | None = Field(default=None, max_length=8)
    stream: bool = Field(default=False, description="是否启用流式响应")


class KnowledgeAnswerResponse(BaseModel):
    """非流式问答结果：完整回答 + 全部引用 + token 用量。"""

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
    """新建会话：可以先开个空会话，也可以直接带个标题。"""

    title: str = Field(
        default="",
        max_length=200,
        description="留空则建「新对话」，首条提问入库后自动改用问题前若干字作标题",
    )


class ChatSessionUpdate(BaseModel):
    """改会话标题（只有这一个字段，传空串直接 422）。"""

    title: str = Field(..., min_length=1, max_length=200)


class ChatSessionOut(BaseModel):
    """对外的会话元信息（不含消息体），列表项也是它。"""

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
        """历史入库只接受对话双方，也不接前端送来的 system。"""
        if v not in {"user", "assistant"}:
            raise ValueError("role must be one of: user / assistant")
        return v


class ChatMessageOut(BaseModel):
    """对外的历史消息：比入库入参多 ``seq`` 与几个降级计数。"""

    id: str
    seq: int
    role: str
    content: str
    images: list[str] = Field(default_factory=list)
    usage: Usage | None = None
    # 因超尺寸而未入库的图片张数（>0 时前端要明说「图片未持久化」而不是默默缺图）
    images_dropped: int = 0
    # 回答时的联网引用（回放能重现「这一答案是查来的、来源是哪几条」）
    web_sources: list[WebSource] = Field(default_factory=list)
    # 当时的思考过程（回放可展开；模型未输出思考时为空串）
    thinking: str = ""
    created_at: float


class ChatMessageListResponse(BaseModel):
    """一个会话的全部历史（同样为了走统一信封而包成对象）。"""

    messages: list[ChatMessageOut] = Field(default_factory=list)


class ChatAppendRequest(BaseModel):
    """一轮问答整批追加入库（按 ``seq`` 续号，不逐条发请求）。"""

    # 上限 20：一轮最多「用户 + 助手」两条，留出修正/重新生成的余量
    messages: list[ChatMessageIn] = Field(..., min_length=1, max_length=20)


class ChatAppendResponse(BaseModel):
    """追加后的会话与消息（带上了服务层算好的 ``id`` / ``seq``）。"""

    session: ChatSessionOut
    messages: list[ChatMessageOut] = Field(default_factory=list)
