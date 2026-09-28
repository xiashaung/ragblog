"""应用配置：从环境变量 / ``.env`` 读取，全进程只有这一份。

每个 LLM provider 各占一块配置（``llm_openai_*`` / ``llm_deepseek_*`` …），
请求里的 ``provider`` 字段决定用哪一块；没指定时用 ``llm_default_provider``。

要注意与本模块的分工：**这里只是兜底来源**，运行时真正生效的配置以数据库为准
（「系统配置」选各功能的 provider、「模型接入」配端点与模型名），由
``services/provider_store`` 读库并缓存在内存里；``.env`` 只在库里没配时兜底。
唯一的例外是 ``rag_vector_dim``、混合检索与智能问数这类结构性参数，仍然只能改 ``.env``。

``get_settings()`` 带 ``lru_cache``：改 ``.env`` 必须重启进程才生效。
"""

from collections.abc import Iterable
from enum import StrEnum  # noqa: F401  (re-exported by app.schemas.ai)
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录（装着 ``app/`` 的那层）；用它解析媒体目录，
# 使上传的图片无论进程从哪个 CWD 启动都落在同一个地方。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 「模型接入」里新建的自定义 provider。由 services.provider_store 在刷新
# 缓存时登记，两个 Provider 枚举共用这一份白名单：请求里的 provider 只接受
# 「内置成员 + 数据库里配过的」，打错的名字仍然会被校验拦下（而不是静默接受）。
_custom_providers: set[str] = set()
# (枚举名, provider) -> 动态成员。不能写进 ``_value2member_map_``：那里删不掉，
# 被删除的 provider 会永远通过校验。
_dynamic_members: dict[tuple[str, str], StrEnum] = {}


def register_custom_providers(names: Iterable[str]) -> None:
    """登记数据库里配置过的 provider id（刷新缓存时全量覆盖，删掉的随即失效）。"""
    _custom_providers.clear()
    _custom_providers.update(str(n).strip().lower() for n in names if str(n).strip())


def custom_providers() -> set[str]:
    """已登记的自定义 provider 名（只读快照）。"""
    return set(_custom_providers)


def custom_provider_member(cls: type[StrEnum], value: object) -> StrEnum | None:
    """把已登记的自定义 provider 变成枚举成员；未登记（如拼错）返回 None。

    同一名字必须复用同一个成员，否则 ``Provider("x") is not Provider("x")``，
    枚举比较与 ``_registry`` 缓存都会错乱。
    """
    name = str(value).strip().lower()
    if not name or name not in _custom_providers:
        return None
    key = (cls.__name__, name)
    member = _dynamic_members.get(key)
    if member is None:
        member = str.__new__(cls, name)
        member._name_ = name.upper()
        member._value_ = name
        _dynamic_members[key] = member
    return member


class Provider(StrEnum):
    """内置支持的 LLM provider。

    与 ``app/schemas/ai.Provider`` 是同一套值，在这里独立定义是为了避开循环导入
    （schemas 要 import 本模块取默认值）。

    内置六个由枚举成员写死；「模型接入」里新建的自定义 provider 由下面的
    ``_missing_`` 动态认（需在白名单里），无需改代码重启。
    """

    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    OLLAMA = "ollama"
    MLX = "mlx"
    MOCK = "mock"

    @classmethod
    def _missing_(cls, value):
        """枚举查不到内置成员时的兜底：只认「模型接入」里配过的自定义 provider。"""
        return custom_provider_member(cls, value)


class ProviderConfig(BaseModel):
    """单个 provider 的端点 + 模型名（调用 LLM 所需的全部参数）。

    ``api_key`` / ``base_url`` 对本地服务（ollama、mlx）和不需要 OpenAI 式
    Key 的 provider 可以留空；留空时 ``OpenAIBackend._auth_key`` 会补一个占位串。
    """

    api_key: str = ""
    base_url: str | None = None
    chat_model: str = ""
    embedding_model: str = ""


class Settings(BaseSettings):
    """全部配置项的容器（字段名 = ``APP_`` 前缀 + 小写字段）。

    分组自上而下就是功能模块：AI/LLM → RAG（向量维度/精排/混合检索）→
    智能问数 → 联网检索 → 管理后台凭据 → 数据库与上传目录。
    每组里都先写「这个开关影响哪条链路」，再加具体参数。
    """

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
    # 全局日志级别（app 与 uvicorn 共用一套格式，见 logging_setup）。
    # 设成 DEBUG 会连带放大所有未单独设级别的三方库日志，线上慎开。
    log_level: str = "INFO"
    # /metrics 的访问令牌（Authorization: Bearer <token> 或 X-Metrics-Token）；
    # 留空 = 不鉴权直接暴露（仅限内网抓取；公网部署务必配）。
    metrics_token: str = ""
    # 默认通配——此时 main.py 会强制关闭 CORS 凭据模式（浏览器本来也不接受
    # 通配 + credentials 的组合）。需要 cookie/Authorization 跨域携带时，
    # 把具体来源逐个列进来。
    cors_origins: list[str] = ["*"]

    # --- AI / LLM ---
    # 请求没带 provider 时的默认值（数据库配了各功能的选择则以数据库为准）。
    llm_default_provider: str = "openai"  # openai|deepseek|qwen|ollama|mlx|mock

    # --- LLM 调用重试与降级 ---
    # 包含首次在内的最大尝试次数。只对瞬时故障（超时/连接失败/429/5xx）重试，
    # 4xx 参数与鉴权错一次即判死——重试不会改变结果，反而拖长响应。
    llm_retry_max_attempts: int = 3
    # 重试间隔指数退避：第 n 次失败后等 min(base * 2^n, max) 秒。
    llm_retry_backoff_base: float = 1.0
    llm_retry_backoff_max: float = 10.0
    # 非流式对话在主 provider 重试耗尽后改问的备胎（留空 = 关闭）。
    # 只影响 chat：流式已吐字不能中途偷换，嵌入换模型会打乱向量空间。
    llm_fallback_provider: str = ""

    # 各 provider 的默认端点与模型名。环境变量按嵌套映射到子字段，
    # 如 APP_LLM_OPENAI_API_KEY、APP_LLM_OPENAI_BASE_URL…
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
        # 本地 mlx-lm / omlx 服务。模型名与端口因机器而异，一律由 .env 提供
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

    # RAG 后台队列的并发 Worker 数。单 Worker 时一个大文档会堵住整条队列；
    # 也不宜过大——向量化是外部 HTTP/本地模型调用，并行太多会打满下游。
    rag_queue_workers: int = 2

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

    # --- 访问本服务用的 API Key ---
    # RBAC 后不再有任何管理接口旁路：它只用作 ``POST /admin/onboard`` 的首次引导密钥
    # （置空则关闭 HTTP 引导，改用 python -m app.bootstrap seed）。
    admin_api_key: str = "admin-dev-key"
    admin_session_ttl_seconds: int = 86_400  # 控制台 Bearer token 的有效期（秒）

    # --- 登录限流（进程内滑窗，见 app/rate_limit.py）---
    # 尝试窗：同一「IP+用户名」在 window 内最多 max_attempts 次登录尝试；
    # 失败锁：lockout 窗内累计 threshold 次口令错误即锁定。多 Worker 时
    # 每进程各记各的，实际阈值约等于配置值 × 进程数（取舍已写在模块头）。
    login_rate_limit_enabled: bool = True
    login_window_seconds: int = 60
    login_max_attempts: int = 10
    login_lock_threshold: int = 5
    login_lockout_seconds: int = 900

    # --- 后台巡检（增长型表的保留策略，见 app/housekeeping.py）---
    # rag_tasks 每次写入操作都落一行、admin_sessions 过期行只靠自然淘汰，
    # 都没兜底清理——长期跑下去只涨不消。启动时跑一次、之后按 interval
    # 周期跑：删终态（success/failed）超保留天的任务行 + 已过期的会话行。
    housekeeping_enabled: bool = True
    housekeeping_interval_seconds: int = 86_400  # 默认 24h 跑一次
    rag_task_retention_days: int = 30  # 终态任务行保留天数

    # --- 数据库（默认 SQLite，生产用 Postgres + pgvector）---
    database_url: str = "sqlite+aiosqlite:///./app.db"
    db_echo: bool = False

    # --- 外部数据源连接串的落盘加密（Fernet）---
    # 留空时由 APP_ADMIN_API_KEY 派生确定性密钥（开箱即用，但生产建议显式配）；
    # 换 key 后存量密文无法解回，需重新录入数据源连接串。
    datasource_secret_key: str = ""

    # --- Uploaded media (images) ---
    # 上传的图片以文件形式落盘、经 StaticFiles 直接对外服务，避免把 base64 blob
    # 塞进数据库。``media_dir`` 可为相对仓库根的路径或绝对路径；``media_url``
    # 是对外访问前缀（与 main.py 的挂载点一致）。
    media_dir: str = "media"
    media_url: str = "/media"

    @property
    def media_root(self) -> Path:
        """媒体根目录的绝对路径（相对值按仓库根解析，绝对值原样用）。"""
        p = Path(self.media_dir)
        return p if p.is_absolute() else _PROJECT_ROOT / p

    def provider_config(self, name: str) -> ProviderConfig:
        """取某个 provider 的 ``.env`` 配置（openai/deepseek/…）。

        注意：这是**兜底路径**，带数据库优先的读取请用
        ``services.llm._provider_cfg``，否则后台改了模型名这里看不到。
        名字不认识时抛 ``KeyError``（自定义 provider 在 .env 里没有对应块）。
        """
        key = f"llm_{name}"
        value = getattr(self, key, None)
        if not isinstance(value, ProviderConfig):
            raise KeyError(f"Unknown provider: {name}")
        return value


@lru_cache
def get_settings() -> Settings:
    """全进程唯一的 Settings 实例（缓存后不再重读 .env，改环境需重启）。

    测试里要换配置时，对读配置的模块 ``monkeypatch.setattr(mod, "get_settings", ...)``，
    而不是真改 .env（conftest 就是这么做的）。
    """
    return Settings()
