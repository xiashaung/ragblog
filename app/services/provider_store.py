"""模型提供者配置的运行时缓存。

聊天 / 知识库问答 / 向量嵌入实际用的 ``base_url`` / ``api_key`` / 模型名，
以数据库里的「模型接入」(``provider_configs``) 与「系统配置」
(``system_configs`` 里各功能选用的 provider) 为准，``.env`` 只在数据库没有
对应配置时兜底。

``LLMBackend`` 是同步调用的（且被进程内缓存），而读库必须是 async，
所以这里做一层「启动时 + 每次配置写入后」刷新的内存缓存，读取端保持同步。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import (
    ProviderConfig,
    get_settings,
    register_custom_providers,
)
from ..models import ProviderConfigRow, SystemConfigRow

logger = logging.getLogger(__name__)

# 各功能在 system_configs 里选择 provider 的 key
FEATURE_KEYS: dict[str, str] = {
    "chat": "chat_provider",
    "rag": "rag_provider",
    "embedding": "embedding_provider",
    "image_embedding": "image_embedding_provider",
    "rerank": "rerank_provider",
}
# 重排序开关的 config key（与 services.system_config 里的常量一致）
KEY_RERANK_ENABLED = "rerank_enabled"

# provider 名 -> 数据库里的原始字段（空串表示「没配」，读取时回落 .env）
_provider_rows: dict[str, dict[str, str]] = {}
# 功能名（chat/rag/...）-> 数据库里选定的 provider
_feature_providers: dict[str, str] = {}
# system_configs 全量快照（开关类配置从这里读，如 rerank_enabled）
_config_values: dict[str, str] = {}
_loaded = False


async def refresh(session: AsyncSession) -> None:
    """从数据库重载缓存；配置写完后调用，改完立即生效（不用重启）。"""
    global _provider_rows, _feature_providers, _config_values, _loaded  # noqa: PLW0603

    rows = (await session.execute(select(ProviderConfigRow))).scalars().all()
    provider_rows: dict[str, dict[str, str]] = {}
    for row in rows:
        provider_rows[row.provider.strip().lower()] = {
            "base_url": row.base_url or "",
            "api_key": row.api_key or "",
            "chat_model": row.chat_model or "",
            "embedding_model": row.embedding_model or "",
            "image_embedding_model": row.image_embedding_model or "",
            "rerank_model": row.rerank_model or "",
        }

    config_rows = (await session.execute(select(SystemConfigRow))).scalars().all()
    by_key = {row.key: (row.value or "").strip() for row in config_rows}
    feature_providers = {
        feature: by_key[key] for feature, key in FEATURE_KEYS.items() if by_key.get(key)
    }

    _provider_rows = provider_rows
    _feature_providers = feature_providers
    _config_values = by_key
    _loaded = True

    # 自定义 provider 要进两个 Provider 枚举的白名单，否则请求体里的
    # provider="my_gateway" 会被 pydantic 判为非法值（422）。
    register_custom_providers(provider_rows)

    # 缓存的后端实例持有旧的 base_url/api_key，配置一变就得丢掉重建
    from . import llm

    llm.reset_registry()
    logger.info(
        "provider 配置缓存已刷新：%d 个 provider，默认 chat=%s",
        len(_provider_rows),
        _feature_providers.get("chat") or "(未配置，回落 .env)",
    )


def reset() -> None:
    """清空缓存（测试用）。"""
    global _provider_rows, _feature_providers, _config_values, _loaded  # noqa: PLW0603

    _provider_rows = {}
    _feature_providers = {}
    _config_values = {}
    _loaded = False


def is_loaded() -> bool:
    """内存缓存是否已从数据库刷过。

    没刷过时各 getter 会全部回落 .env（启动前 / 测试里直接调服务层就是这样），
    可用它区分「真没配」与「还没读库」。
    """
    return _loaded


def known_providers() -> list[str]:
    """数据库里配置过的 provider（按名称排序）。"""
    return sorted(_provider_rows)


def get_feature_provider(feature: str) -> str | None:
    """某功能在「系统配置」里选定的 provider；没配置返回 None。"""
    return _feature_providers.get(feature)


def get_provider_config(name: str) -> ProviderConfig | None:
    """取 provider 的实际调用参数：数据库优先，缺项回落 ``.env``。

    数据库里完全没有这个 provider 时返回 None（调用方再回落 settings）。
    """
    row = _provider_rows.get(name.strip().lower())
    if row is None:
        return None

    settings = get_settings()
    try:
        fallback = settings.provider_config(name)
    except KeyError:
        # 自定义 provider 在 .env 里没有对应块，全部以数据库为准
        fallback = ProviderConfig()

    # 空串按「未填写」处理：留 .env 的值兜底，避免清空一个字段就把 endpoint 弄丢
    return ProviderConfig(
        api_key=row["api_key"] or fallback.api_key,
        base_url=row["base_url"] or fallback.base_url,
        chat_model=row["chat_model"] or fallback.chat_model,
        embedding_model=row["embedding_model"] or fallback.embedding_model,
        image_embedding_model=row["image_embedding_model"]
        or fallback.image_embedding_model,
    )


def get_image_embedding_model(name: str) -> str:
    """provider 上挂的图片 VL 嵌入模型名（没配置返回空串）。"""
    row = _provider_rows.get(name.strip().lower())
    return row["image_embedding_model"] if row else ""


def get_rerank_model(name: str) -> str:
    """provider 上挂的重排序模型名（没配置返回空串）。"""
    row = _provider_rows.get(name.strip().lower())
    return row["rerank_model"] if row else ""


def get_config(key: str) -> str | None:
    """任意 system_configs 的值；没配置返回 None（调用方回落 ``.env``）。"""
    return _config_values.get(key) or None


@dataclass(frozen=True)
class RerankSettings:
    """重排序的实际调用参数（数据库优先，``.env`` 兜底）。"""

    enabled: bool
    base_url: str
    api_key: str
    model: str


def get_rerank_config() -> RerankSettings:
    """重排序用哪个服务：「系统配置」的 rerank_provider + 「模型接入」的端点。

    数据库里没配 rerank provider（或该 provider 没有 base_url）时，仍然用
    ``.env`` 的 ``APP_RAG_RERANK_*``，保证老部署升级后行为不变。
    """
    settings = get_settings()
    raw_enabled = _config_values.get(KEY_RERANK_ENABLED)
    enabled = (
        raw_enabled.strip().lower() in ("true", "1", "yes", "on")
        if raw_enabled and raw_enabled.strip()
        else settings.rag_rerank_enabled
    )

    provider = _feature_providers.get("rerank")
    cfg = get_provider_config(provider) if provider else None
    if cfg is not None and cfg.base_url:
        # 模型名取 provider 上挂的 rerank_model，没填则回落 .env
        return RerankSettings(
            enabled=enabled,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=get_rerank_model(provider) or settings.rag_rerank_model,
        )
    return RerankSettings(
        enabled=enabled,
        base_url=settings.rag_rerank_base_url,
        api_key=settings.rag_rerank_api_key,
        model=settings.rag_rerank_model,
    )
