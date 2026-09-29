"""博客模块的请求 / 响应模型：只描述数据形状，业务规则在服务层。

三组形状：
- 后台管理（``/admin/blogs/*``）：列表项带 ``status`` / ``knowledge_id`` 等运维字段；
- 前台公开（``/api/v1/blog/*``）：只出已发布文章，不暴露内部 ID 与索引状态；
- AI 检索 / 问答 / 对话：引用（``BlogCitation``）以文章 slug 为锚点，
  对话流式块 ``BlogChatChunk`` 复用知识库对话的 ``ChatChunk`` 字段，另加一行引用。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .ai import ChatChunk, ChatMessage


class BlogCategoryCreate(BaseModel):
    """后台新建分类；slug 留空时由名称生成（中文剥空则随机兜底）。"""

    name: str = Field(..., min_length=1, max_length=50)
    slug: str | None = Field(default=None, max_length=80)
    sort_order: int = 0


class BlogCategoryUpdate(BaseModel):
    """局部更新分类（exclude_unset 语义）。"""

    name: str | None = Field(default=None, min_length=1, max_length=50)
    slug: str | None = Field(default=None, max_length=80)
    sort_order: int | None = None


class BlogCategoryItem(BaseModel):
    """分类列表项；``post_count`` 由服务层聚合（后台算全部，前台只算已发布）。"""

    id: str
    name: str
    slug: str
    sort_order: int
    post_count: int = 0
    created_at: float
    updated_at: float


class BlogCategoryListResponse(BaseModel):
    """分页外的全集列表：包成对象走统一信封（与标签云同一形状）。"""

    items: list[BlogCategoryItem] = Field(default_factory=list)


class BlogCategoryBrief(BaseModel):
    """嵌在文章里的分类摘要：只给展示/跳转要的两样，不暴露内部 ID。"""

    name: str
    slug: str


class BlogPublicCategoryItem(BaseModel):
    """前台分类导航项：同样不出内部 ID，只给名称、slug 与已发布篇数。"""

    name: str
    slug: str
    post_count: int = 0


class BlogPublicCategoryListResponse(BaseModel):
    items: list[BlogPublicCategoryItem] = Field(default_factory=list)


class BlogPostCreate(BaseModel):
    """后台新建文章；slug 留空时由标题生成（生成不出可用字符则用随机后缀）。"""

    title: str = Field(..., min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=150)
    summary: str = Field(default="", max_length=500)
    content: str = Field(default="", description="正文 Markdown，草稿可以为空")
    cover_url: str | None = Field(default=None, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=10)
    # 归属分类（None = 未分类）；存在性由服务层校验
    category_id: str | None = None
    featured: bool = False

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, v: list[str]) -> list[str]:
        """去空白、去空项、去重（保序），单个标签限长 30。"""
        out: list[str] = []
        for tag in v:
            t = tag.strip()[:30]
            if t and t not in out:
                out.append(t)
        return out


class BlogPostUpdate(BaseModel):
    """局部更新：没传的字段保持原样（``exclude_unset`` 语义）。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=150)
    summary: str | None = Field(default=None, max_length=500)
    content: str | None = None
    cover_url: str | None = Field(default=None, max_length=500)
    tags: list[str] | None = Field(default=None, max_length=10)
    # 没传 = 不改；显式传 null = 移出分类（exclude_unset 区分这两种）
    category_id: str | None = None
    featured: bool | None = None

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, v: list[str] | None) -> list[str] | None:
        """与创建同一清洗规则（None 表示不改，不清洗）。"""
        if v is None:
            return None
        out: list[str] = []
        for tag in v:
            t = tag.strip()[:30]
            if t and t not in out:
                out.append(t)
        return out


class BlogPostItem(BaseModel):
    """后台列表 / 详情共用的文章形状（详情额外带 ``content``）。"""

    id: str
    slug: str
    title: str
    summary: str
    cover_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    # 归属分类：id 供后台表单回填，category 供列表直接展示名称
    category_id: str | None = None
    category: BlogCategoryBrief | None = None
    status: str
    featured: bool
    view_count: int
    # 发布同步出的知识文档；为 None 说明「已发布但没索引」（如索引被手动删了）
    knowledge_id: str | None = None
    published_at: float | None = None
    created_at: float
    updated_at: float


class BlogPostDetail(BlogPostItem):
    """后台详情：多一个正文全文。"""

    content: str = ""


class BlogPostListResponse(BaseModel):
    """分页列表（包成对象走统一信封，避免裸数组）。"""

    total: int
    offset: int
    limit: int
    items: list[BlogPostItem] = Field(default_factory=list)


class BlogPublicItem(BaseModel):
    """前台列表项：不带内部 ID 与索引状态，只给展示要的东西。"""

    slug: str
    title: str
    summary: str
    cover_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    # 分类只给名称与 slug（内部 ID 不外泄）
    category: BlogCategoryBrief | None = None
    featured: bool
    published_at: float | None = None
    view_count: int


class BlogPublicDetail(BlogPublicItem):
    """前台文章页：正文 Markdown（渲染由前端负责）。"""

    content: str = ""
    updated_at: float


class BlogPublicListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[BlogPublicItem] = Field(default_factory=list)


class BlogNavItem(BaseModel):
    """上/下篇导航项：只给 slug 与标题。"""

    slug: str
    title: str


class BlogNeighborsResponse(BaseModel):
    """文章页上/下篇：已到最早/最新一端时对应方向为 null。"""

    prev: BlogNavItem | None = None
    next: BlogNavItem | None = None


class BlogTagItem(BaseModel):
    name: str
    count: int


class BlogTagListResponse(BaseModel):
    """标签云（聚合已发布文章）；同样包对象防止裸数组歧义。"""

    items: list[BlogTagItem] = Field(default_factory=list)


# --- AI：语义搜索 / 问答 / 对话 ---


class BlogSearchRequest(BaseModel):
    """前台 AI 搜索的裸检索：只召回文章，不生成回答。"""

    query: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=10)


class BlogCitation(BaseModel):
    """一条引用：以前台可跳转的 slug 为锚点，``index`` 对应正文角标 [n]。"""

    index: int
    slug: str
    title: str
    score: float
    snippet: str


class BlogSearchResponse(BaseModel):
    query: str
    total: int
    items: list[BlogCitation] = Field(default_factory=list)


class BlogAnswerResponse(BaseModel):
    """一次性 AI 问答：完整回答 + 引用文章。"""

    query: str
    answer: str
    citations: list[BlogCitation] = Field(default_factory=list)


class BlogChatRequest(BaseModel):
    """AI 对话搜索：前端携带整段多轮历史（公开接口不落库）。"""

    messages: list[ChatMessage] = Field(..., min_length=1, max_length=20)

    @field_validator("messages")
    @classmethod
    def _text_only(cls, v: list[ChatMessage]) -> list[ChatMessage]:
        """公开对话只收纯文本 user/assistant 消息：多模态与工具回合不属于这里。"""
        for m in v:
            if m.role not in ("user", "assistant"):
                raise ValueError("博客对话只接受 user / assistant 消息")
            if not isinstance(m.content, str) or not m.content.strip():
                raise ValueError("消息内容必须是非空纯文本")
            if len(m.content) > 8000:
                raise ValueError("单条消息长度不能超过 8000 字符")
        if v[-1].role != "user":
            raise ValueError("最后一条消息必须是用户提问")
        return v


class BlogChatChunk(ChatChunk):
    """博客对话流的一行（NDJSON）：复用 ChatChunk 字段，另带引用。

    ``citations`` 只在流的第一行给出（本轮检索命中的文章），后续行为空列表。
    """

    citations: list[BlogCitation] = Field(default_factory=list)
