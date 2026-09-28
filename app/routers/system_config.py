"""系统配置与模型接入接口（读需 ``system:read``，写需 ``system:manage``）。

两类配置都存库，改完不用重启：

- 功能路由（``system_configs``）：聊天 / 嵌入 / 精排各自用哪个 provider；
- 模型接入（``provider_configs``）：每个端点的 base_url / key / 模型名。

写入后都必须调 :func:`provider_store.refresh` 把新值读回内存（并间接
重置 ``llm`` 的后端注册表），否则运行中的调用仍拿着旧的端点与模型名。
"""

import os
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, deps
from ..config import Provider, get_settings
from ..db import get_db
from ..models import ProviderConfigRow
from ..rbac import Permission
from ..schemas.admin import ConfigItem, ConfigListResponse, ConfigUpdateRequest
from ..services import provider_store, rag, rag_queue, system_config
from ..services.rbac_service import Principal


class ProviderConfigSchema(BaseModel):
    """模型提供者配置 Schema。"""
    provider: str = ""
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    chat_model: str = ""
    embedding_model: str = ""
    rerank_model: str = ""

router = APIRouter(prefix="/system/config", tags=["系统配置"])


@router.get("/", response_model=ConfigListResponse)
async def list_configs(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_READ)),
) -> ConfigListResponse:
    """获取所有系统配置。"""
    configs = await system_config.SystemConfig.get_all(session)
    return ConfigListResponse(configs=[
        ConfigItem(key=k, value=v, description="") for k, v in configs.items()
    ])


@router.get("/models", response_model=dict[str, str])
async def get_all_models(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_READ)),
) -> dict[str, str]:
    """获取所有模型相关的配置（对话、问答、向量、重排序）。

    数据库里没配的功能回显 .env 的实际默认值，避免页面把未配置误读成 ``mock``。
    """
    settings = get_settings()
    fallback = settings.llm_default_provider
    Cfg = system_config.SystemConfig
    all_configs = await Cfg.get_all(session)
    return {
        "chat_provider": all_configs.get(Cfg.KEY_CHAT_PROVIDER, fallback),
        "rag_provider": all_configs.get(Cfg.KEY_RAG_PROVIDER, fallback),
        "embedding_provider": all_configs.get(Cfg.KEY_EMBEDDING_PROVIDER, fallback),
        "rerank_enabled": all_configs.get(
            Cfg.KEY_RERANK_ENABLED, str(settings.rag_rerank_enabled)
        ),
        "rerank_provider": all_configs.get(Cfg.KEY_RERANK_PROVIDER, fallback),
        # 重排序模型是全局的一项（选中 provider 上挂的名字，没配回落 .env），
        # 不随某个 provider 走，所以直接给出实际调用用的那个名字。
        "rerank_model": provider_store.get_rerank_config().model,
    }


@router.get("/chat-provider", response_model=str)
async def get_chat_provider(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_READ)),
) -> str:
    """获取 AI 对话的默认 LLM provider。
    
    支持的值：openai, deepseek, qwen, ollama, mlx, mock，以及「模型接入」里新建的 provider
    """
    value = await system_config.SystemConfig.get(
        session,
        system_config.SystemConfig.KEY_CHAT_PROVIDER,
        default=get_settings().llm_default_provider,
    )
    return value


@router.get("/rag-provider", response_model=str)
async def get_rag_provider(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_READ)),
) -> str:
    """获取知识库问答的默认 LLM provider。
    
    支持的值：openai, deepseek, qwen, ollama, mlx, mock，以及「模型接入」里新建的 provider
    """
    value = await system_config.SystemConfig.get(
        session,
        system_config.SystemConfig.KEY_RAG_PROVIDER,
        default=get_settings().llm_default_provider,
    )
    return value


@router.patch("/chat-provider", response_model=str)
async def set_chat_provider(
    req: ConfigUpdateRequest,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_MANAGE)),
) -> str:
    """设置 AI 对话的默认 LLM provider。
    
    支持的值：openai, deepseek, qwen, ollama, mlx, mock
    """
    if not req.value:
        raise HTTPException(status_code=400, detail="provider 不能为空")
    
    row = await system_config.SystemConfig.set(
        session,
        system_config.SystemConfig.KEY_CHAT_PROVIDER,
        req.value,
        description="AI 对话使用的 LLM provider（如：openai, deepseek, qwen, ollama, mlx）",
    )
    audit.event(
        audit.Actor.from_principal(principal), "config.update", row.key, value=req.value
    )
    return row.value


@router.patch("/rag-provider", response_model=str)
async def set_rag_provider(
    req: ConfigUpdateRequest,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_MANAGE)),
) -> str:
    """设置知识库问答的默认 LLM provider。
    
    支持的值：openai, deepseek, qwen, ollama, mlx, mock
    """
    if not req.value:
        raise HTTPException(status_code=400, detail="provider 不能为空")
    
    row = await system_config.SystemConfig.set(
        session,
        system_config.SystemConfig.KEY_RAG_PROVIDER,
        req.value,
        description="知识库问答使用的 LLM provider（可独立于对话模型）",
    )
    audit.event(
        audit.Actor.from_principal(principal), "config.update", row.key, value=req.value
    )
    return row.value


@router.patch("/rerank-enabled", response_model=bool)
async def toggle_rerank(
    req: ConfigUpdateRequest,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_MANAGE)),
) -> bool:
    """启用/禁用重排序服务。"""
    enabled = req.value.lower() == "true"

    await system_config.SystemConfig.set(
        session,
        system_config.SystemConfig.KEY_RERANK_ENABLED,
        str(enabled),
        description="是否启用重排序服务",
    )
    audit.event(
        audit.Actor.from_principal(principal),
        "config.update",
        system_config.SystemConfig.KEY_RERANK_ENABLED,
        value=str(enabled),
    )
    return enabled


@router.get("/rerank-provider", response_model=str)
async def get_rerank_provider(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_READ)),
) -> str:
    """获取重排序服务的默认 provider。
    
    支持的值：openai, deepseek, qwen, ollama, mlx, mock，以及「模型接入」里新建的 provider
    """
    value = await system_config.SystemConfig.get(
        session,
        system_config.SystemConfig.KEY_RERANK_PROVIDER,
        default=get_settings().llm_default_provider,
    )
    return value


@router.patch("/rerank-provider", response_model=str)
async def set_rerank_provider(
    req: ConfigUpdateRequest,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_MANAGE)),
) -> str:
    """设置重排序服务的默认 provider。
    
    支持的值：openai, deepseek, qwen, ollama, mlx, mock
    """
    if not req.value:
        raise HTTPException(status_code=400, detail="provider 不能为空")
    
    row = await system_config.SystemConfig.set(
        session,
        system_config.SystemConfig.KEY_RERANK_PROVIDER,
        req.value,
        description="重排序服务使用的 LLM provider",
    )
    audit.event(
        audit.Actor.from_principal(principal), "config.update", row.key, value=req.value
    )
    return row.value


@router.patch("/{key}", response_model=str)
async def update_config_by_key(
    key: str,
    req: ConfigUpdateRequest,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_MANAGE)),
) -> str:
    """通过 key 更新任意系统配置。
    
    支持的 key：embedding_provider
    """
    # 验证 key 是否在允许的列表中
    allowed_keys = [
        system_config.SystemConfig.KEY_EMBEDDING_PROVIDER,
    ]
    
    if key not in allowed_keys:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的配置项：{key}。支持的 key: {', '.join(allowed_keys)}"
        )
    
    descriptions = {
        system_config.SystemConfig.KEY_EMBEDDING_PROVIDER: (
            "向量嵌入使用的 embedding provider（用于知识库向量化）"
        ),
    }

    # 换嵌入模型前先读旧值：只有真的改了才触发全库重建（同值重复保存不应惊动整库）。
    old_value = await system_config.SystemConfig.get(
        session, key, default=get_settings().llm_default_provider
    )

    row = await system_config.SystemConfig.set(
        session,
        key,
        req.value,
        description=descriptions.get(key),
    )
    audit.event(audit.Actor.from_principal(principal), "config.update", row.key, value=req.value)

    # 嵌入模型变更：旧向量与新模型不再匹配，整库置待重建并强制重建。
    # 先提交 status 变更，worker 的独立会话才读得到；无原文的老文档直接标失败。
    if (
        key == system_config.SystemConfig.KEY_EMBEDDING_PROVIDER
        and row.value != old_value
    ):
        await rag.mark_docs_for_full_rebuild(session)
        await session.commit()
        if os.environ.get("APP_TEST_MODE", "false").lower() == "true":
            # 测试同步执行：直接跑完全库重建，结果确定（不依赖后台 worker）
            await rag.reindex_all_docs(session)
        else:
            await rag_queue.get_queue().enqueue(
                session,
                rag_queue.RAGTask(
                    task_type=rag_queue.TaskType.REINDEX_ALL,
                    payload={},
                    created_at=time.time(),
                ),
            )
    return row.value


@router.get("/providers", response_model=dict[str, dict])
async def get_providers(
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_READ)),
) -> dict[str, dict]:
    """获取所有模型提供者的配置（从数据库）。
    """
    # 优先从数据库读取
    from sqlalchemy import select
    
    stmt = select(ProviderConfigRow)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    
    providers = {}
    for row in rows:
        providers[row.provider] = {
            "name": row.name,
            "base_url": row.base_url or "",
            "api_key": row.api_key or "",
            "chat_model": row.chat_model or "",
            "embedding_model": row.embedding_model or "",
            "rerank_model": row.rerank_model or "",
        }
    
    # 如果数据库为空，返回默认配置
    if not providers:
        settings = get_settings()
        for name in Provider:
            cfg = settings.provider_config(name.value)
            providers[name.value] = {
                "name": name.value.capitalize(),
                "base_url": cfg.base_url or "",
                "api_key": cfg.api_key or "",
                "chat_model": cfg.chat_model or "",
                "embedding_model": cfg.embedding_model or "",
                "rerank_model": "",
            }
    
    return providers


@router.post("/providers", response_model=dict)
async def create_provider(
    data: ProviderConfigSchema,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_MANAGE)),
) -> dict:
    """创建新的提供者配置。
    """
    provider_id = data.provider.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", provider_id):
        raise HTTPException(
            status_code=400,
            detail="Provider ID 只能包含小写字母、数字、下划线，且以字母开头",
        )

    # 检查 provider 是否已存在
    stmt = select(ProviderConfigRow).where(ProviderConfigRow.provider == provider_id)
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        raise HTTPException(status_code=400, detail=f"提供者 '{provider_id}' 已存在")

    # 创建新配置
    new_config = ProviderConfigRow(
        provider=provider_id,
        name=data.name,
        base_url=data.base_url,
        api_key=data.api_key,
        chat_model=data.chat_model,
        embedding_model=data.embedding_model,
        rerank_model=data.rerank_model or "",
    )
    session.add(new_config)
    await session.commit()
    await provider_store.refresh(session)
    # 审计只记 provider id，绝不落 base_url / api_key 等敏感值
    audit.event(
        audit.Actor.from_principal(principal), "provider.create", provider_id, name=data.name
    )
    return {"status": "success", "message": f"{data.name} 配置已创建"}


@router.post("/providers/{provider}", response_model=dict)
async def save_provider(
    provider: str,
    data: ProviderConfigSchema,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_MANAGE)),
) -> dict:
    """保存指定提供者的配置到数据库（upsert：内置 provider 首次保存时落库）。
    """
    # 查找现有记录
    stmt = select(ProviderConfigRow).where(ProviderConfigRow.provider == provider)
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        # 更新现有配置
        existing.name = data.name
        existing.base_url = data.base_url
        existing.api_key = data.api_key
        existing.chat_model = data.chat_model
        existing.embedding_model = data.embedding_model
        existing.rerank_model = data.rerank_model or ""
    else:
        # 内置 provider 首次保存：新建记录
        session.add(ProviderConfigRow(
            provider=provider,
            name=data.name or provider,
            base_url=data.base_url,
            api_key=data.api_key,
            chat_model=data.chat_model,
            embedding_model=data.embedding_model,
            rerank_model=data.rerank_model or "",
        ))

    await session.commit()
    await provider_store.refresh(session)
    # 审计只记 provider id，绝不落 base_url / api_key 等敏感值
    audit.event(
        audit.Actor.from_principal(principal), "provider.update", provider,
        name=data.name or provider,
    )
    return {"status": "success", "message": f"{data.name or provider} 配置已更新"}


@router.delete("/providers/{provider}", response_model=dict)
async def delete_provider(
    provider: str,
    session: AsyncSession = Depends(get_db),
    principal: Principal = Depends(deps.require_permission(Permission.SYSTEM_MANAGE)),
) -> dict:
    """删除指定的提供者配置。
    """
    # 检查是否存在
    stmt = select(ProviderConfigRow).where(ProviderConfigRow.provider == provider)
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    
    if not existing:
        raise HTTPException(status_code=404, detail=f"提供者 '{provider}' 不存在")
    
    # 删除配置
    name = existing.name
    await session.delete(existing)
    await session.commit()
    await provider_store.refresh(session)
    audit.event(
        audit.Actor.from_principal(principal), "provider.delete", provider, name=name
    )
    return {"status": "success", "message": f"{name} 配置已删除"}
