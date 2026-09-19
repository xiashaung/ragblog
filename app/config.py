"""Application settings, loaded from environment variables / .env file.

Each LLM provider has its own config block (``llm_openai_*``,
``llm_deepseek_*``, etc). The router can target any provider via the
request ``provider`` field; the per-request provider falls back to
``llm_default_provider``.
"""

from enum import StrEnum  # noqa: F401  (re-exported by app.schemas.ai)
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root (the directory that holds ``app/``); used to resolve the media dir
# so uploaded images land in a stable place regardless of the process CWD.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Provider(StrEnum):
    """Supported LLM providers. Mirrors ``app/schemas/ai.Provider``; defined
    here to avoid a circular import (schemas import config for the default)."""

    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    OLLAMA = "ollama"
    MLX = "mlx"
    MOCK = "mock"


class ProviderConfig(BaseModel):
    """Per-provider endpoint + model config.

    ``api_key`` and ``base_url`` are optional for local providers (ollama, mlx)
    and for providers that don't need an OpenAI-style API key.
    """

    api_key: str = ""
    base_url: str | None = None
    chat_model: str = ""
    embedding_model: str = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="APP_",
        # 嵌套风格的标准写法是双下划线（APP_LLM_MLX__CHAT_MODEL）；
        # 单下划线的历史写法由下面显式声明的 llm_<provider>_<part> 字段承接。
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_name: str = "FastAPI App"
    version: str = "0.1.0"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = ["*"]

    # --- AI / LLM ---
    # Default provider used when the request does not specify one.
    llm_default_provider: str = "openai"  # openai|deepseek|qwen|ollama|mlx|mock

    # Per-provider configs. Env vars map to the sub-field via nesting,
    # e.g. APP_LLM_OPENAI_API_KEY, APP_LLM_OPENAI_BASE_URL, ...
    llm_openai: ProviderConfig = ProviderConfig(
        base_url="https://api.openai.com/v1",
        chat_model="gpt-4o-mini",
        embedding_model="text-embedding-3-small",
    )
    llm_deepseek: ProviderConfig = ProviderConfig(
        base_url="https://api.deepseek.com/v1",
        chat_model="deepseek-chat",
        embedding_model="deepseek-embed",
    )
    llm_qwen: ProviderConfig = ProviderConfig(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        chat_model="qwen-plus",
        embedding_model="text-embedding-v3",
    )
    llm_ollama: ProviderConfig = ProviderConfig(
        base_url="http://localhost:11434/v1",
        chat_model="llama3.1",
        embedding_model="nomic-embed-text",
    )
    llm_mlx: ProviderConfig = ProviderConfig(
        # Local mlx-lm / omlx server. 模型名与端口因机器而异，一律由 .env 提供
        # （APP_LLM_MLX_BASE_URL / _CHAT_MODEL / _EMBEDDING_MODEL / _API_KEY），
        # 代码只留通用默认兜底，不写死任何具体环境的值。
        base_url="http://localhost:8080/v1",
        chat_model="",
        embedding_model="",
        # 本地服务通常接受任意 Bearer；未配 env 时留空即可。
        api_key="",
    )
    llm_mock: ProviderConfig = ProviderConfig(
        chat_model="mock-chat",
        embedding_model="mock-embed",
    )

    # --- 单下划线扁平写法兼容（历史 .env 风格）---
    # pydantic-settings 默认不会把 APP_LLM_MLX_CHAT_MODEL 拆给嵌套字段，
    # 没有这组声明时它会被 extra="ignore" 静默丢弃，改 .env 不生效。
    # 这里把每个 provider 的四个可覆盖项显式接住，再由下方校验器合并回嵌套配置。
    llm_openai_api_key: str | None = None
    llm_openai_base_url: str | None = None
    llm_openai_chat_model: str | None = None
    llm_openai_embedding_model: str | None = None
    llm_deepseek_api_key: str | None = None
    llm_deepseek_base_url: str | None = None
    llm_deepseek_chat_model: str | None = None
    llm_deepseek_embedding_model: str | None = None
    llm_qwen_api_key: str | None = None
    llm_qwen_base_url: str | None = None
    llm_qwen_chat_model: str | None = None
    llm_qwen_embedding_model: str | None = None
    llm_ollama_api_key: str | None = None
    llm_ollama_base_url: str | None = None
    llm_ollama_chat_model: str | None = None
    llm_ollama_embedding_model: str | None = None
    llm_mlx_api_key: str | None = None
    llm_mlx_base_url: str | None = None
    llm_mlx_chat_model: str | None = None
    llm_mlx_embedding_model: str | None = None
    llm_mock_api_key: str | None = None
    llm_mock_base_url: str | None = None
    llm_mock_chat_model: str | None = None
    llm_mock_embedding_model: str | None = None

    _LLM_PROVIDER_NAMES: ClassVar[tuple[str, ...]] = (
        "openai",
        "deepseek",
        "qwen",
        "ollama",
        "mlx",
        "mock",
    )
    _LLM_CONFIG_PARTS: ClassVar[tuple[str, ...]] = (
        "api_key",
        "base_url",
        "chat_model",
        "embedding_model",
    )

    @model_validator(mode="after")
    def _merge_flat_llm_env(self) -> "Settings":
        """把扁平环境变量值合并进对应 provider 的嵌套配置（扁平值优先）。"""
        for name in self._LLM_PROVIDER_NAMES:
            cfg = getattr(self, f"llm_{name}")
            for part in self._LLM_CONFIG_PARTS:
                value = getattr(self, f"llm_{name}_{part}", None)
                if value is not None:
                    setattr(cfg, part, value)
        return self

    # --- RAG / 向量检索 ---
    # ``knowledge_chunks.vector`` 的列维度（Postgres 上是 vector(<dim>)）。
    # 换 embedding 模型导致维度变化时，改这里并重跑 ``alembic upgrade head``
    # （迁移会把维度不符的旧向量备份后置空，之后需要重建索引）。
    rag_vector_dim: int = 1024

    # --- RAG 精排（cross-encoder rerank）---
    # 启用后检索变成两段式：向量宽召回 → 调 rerank 模型精排取 top_k。
    # 端点采用 OpenAI/Cohere 风格的 ``POST {base}/rerank``（Xinference、
    # SiliconFlow、TEI 兼容层等均提供）。默认关闭，不开启时行为与以前一致。
    rag_rerank_enabled: bool = False
    # base_url 到版本段即可（如 http://localhost:8089/v1），实际请求会拼 /rerank。
    rag_rerank_base_url: str = ""
    rag_rerank_model: str = "bge-reranker-v2-m3"
    rag_rerank_api_key: str = ""  # 本地服务可留空
    # 精排前从向量库多拉多少候选（recall_k = top_k * multiplier），供 rerank 挑选。
    rag_rerank_recall_multiplier: int = 5
    # 低于此精排分的候选直接丢弃（宁缺毋滥）；rerank 分标度因模型而异，默认 0 不过滤。
    rag_rerank_min_score: float = 0.0
    rag_rerank_timeout: float = 10.0

    # --- RAG 混合检索（向量 + 全文词法，RRF 融合）---
    # 启用后检索变三段：向量召回 + Postgres 词法召回 → RRF 融合 →（可选）精排。
    # 词法腿用 pg_trgm 的三元组相似度（word_similarity），它对中文不依赖分词
    # 也能命中关键词（原生 tsvector 会把无空格中文当成单 token，不适合本项目）。
    # 仅在 Postgres 上生效；SQLite（测试）自动退回纯向量。默认关闭。
    rag_hybrid_enabled: bool = False
    # 词法召回最多扫多少分片（按相似度降序），与向量召回的 recall_k 各自取。
    rag_hybrid_text_topk: int = 20
    # RRF 融合公式 1/(k+rank) 的平滑常量，经验值 60；越大越弱化头部排名优势。
    rag_hybrid_rrf_k: int = 60
    # 向量腿与词法腿在 RRF 中的权重（向量权重，词法为 1-vector_weight）。
    rag_hybrid_vector_weight: float = 0.5

    # --- 数据分析（Text-to-SQL 智能问数）---
    # 独立的「连库→自然语言→只读 SQL→执行→AI 结论」功能。总开关，关闭后接口返回 403。
    analytics_enabled: bool = True
    # 单次问数最多返回多少行（超出截断并置 truncated），也用于喂给总结模型的行数。
    analytics_max_rows: int = 200
    # Postgres 语句超时（秒），避免烂 SQL 跑挂库；SQLite 侧仅用于提示。
    analytics_timeout_seconds: float = 15.0
    # 永不对外暴露、也不进 LLM 上下文的数据表（系统/迁移表）。
    analytics_denied_tables: list[str] = ["alembic_version"]
    # 敏感列：即使被 SELECT * 命中，也会从 schema 上下文与结果行里抹掉（防口令/令牌外泄）。
    analytics_secret_columns: list[str] = [
        "password",
        "password_hash",
        "token",
        "token_hash",
        "api_key",
        "secret",
    ]

    # --- 联网检索（给对话模型挂 web_search / fetch_page 工具）---
    # 总开关：关掉后 /ai/chat 不再向模型声明工具，模型只能凭参数知识回答。
    web_search_enabled: bool = True
    # 搜索源：tavily / serper 需要 API key（两家都有免费额度）；选定的源没配 key 时
    # 自动退回 bing 网页抓取——免 key 能跑，但 HTML 一改版就哑，只当兜底。
    web_search_provider: str = "tavily"
    # 只从 .env 读（APP_WEB_SEARCH_API_KEY），代码里不留默认值。
    web_search_api_key: str = ""
    # 走自建网关/代理时覆盖，留空用各家官方地址。
    web_search_base_url: str = ""
    # 单次搜索返回多少条，也是回喂给模型的条数上限。
    web_search_max_results: int = 5
    web_search_timeout_seconds: float = 12.0
    # 摘要进 prompt 前按此长度截断（不截会让 token 随条数线性膨胀）。
    web_search_snippet_chars: int = 500
    # 一次提问里允许多少个「模型要求调工具」的回合，超出就强制它基于已有信息作答。
    web_search_max_rounds: int = 3
    # 相同查询在该秒数内复用缓存，避免重复烧搜索额度。
    web_search_cache_ttl_seconds: int = 600
    # fetch_page 工具的单页抓取约束（与知识库录入共用同一个解析器）。
    web_fetch_timeout_seconds: float = 15.0
    web_fetch_max_chars: int = 6000
    # 放开后才允许抓内网地址（调试用）。默认严格拒绝私网/回环，防模型被诱导打 SSRF。
    web_fetch_allow_private: bool = False

    # --- API keys for accessing THIS service ---
    # RBAC 后不再有任何管理接口旁路：它只用作 ``POST /admin/onboard`` 的首次引导密钥
    # （置空则关闭 HTTP 引导，改用 python -m app.bootstrap seed）。
    admin_api_key: str = "admin-dev-key"
    default_request_quota: int = 100  # requests per hour per key
    admin_session_ttl_seconds: int = 86_400  # admin dashboard bearer token lifetime

    # --- Database (SQLite by default) ---
    database_url: str = "sqlite+aiosqlite:///./app.db"
    db_echo: bool = False

    # --- Uploaded media (images) ---
    # 上传的图片以文件形式落盘、经 StaticFiles 直接对外服务，避免把 base64 blob
    # 塞进数据库。``media_dir`` 可为相对仓库根的路径或绝对路径；``media_url``
    # 是对外访问前缀（与 main.py 的挂载点一致）。
    media_dir: str = "media"
    media_url: str = "/media"

    @property
    def media_root(self) -> Path:
        p = Path(self.media_dir)
        return p if p.is_absolute() else _PROJECT_ROOT / p

    # --- Backward-compat aliases (used by tests / legacy .env) ---
    # These mirror the per-provider values; prefer the nested fields.
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_base_url: str | None = None
    llm_chat_model: str = "gpt-4o-mini"
    llm_embedding_model: str = "text-embedding-3-small"

    def provider_config(self, name: str) -> ProviderConfig:
        """Return the config for a provider name (openai/deepseek/...)."""
        key = f"llm_{name}"
        value = getattr(self, key, None)
        if not isinstance(value, ProviderConfig):
            raise KeyError(f"Unknown provider: {name}")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
