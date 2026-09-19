"""Service layer.

- ``llm``            — LLM gateway (OpenAI-compatible or mock)
- ``rag``            — RAG: chunking + async SQLite-backed vector store
- ``analytics``      — 数据分析（Text-to-SQL 智能问数，只读）
- ``chat_history``   — AI 对话会话 / 消息持久化（仅本人可见）
- ``web_search``     — 联网检索的搜索源适配层（tavily / serper / bing）
- ``chat_tools``     — 给对话模型挂的联网工具与「生成→工具→再生成」循环
- ``datasource``     — 外部数据源登记 / 掩码 / 连通性测试（问数可选目标库）
- ``web_page``       — fetch a URL and reduce it to text for ingestion
- ``rbac_service``   — 后台角色 / 账号 / 权限解析（RBAC）
- ``user_service`` / ``item_service`` — async DB-backed repositories
"""

from . import (
    admin_service,
    analytics,
    chat_history,
    chat_tools,
    datasource,
    document_parser,
    item_service,
    llm,
    rag,
    rbac_service,
    user_service,
    web_page,
    web_search,
)

__all__ = [
    "admin_service",
    "analytics",
    "chat_history",
    "chat_tools",
    "datasource",
    "document_parser",
    "item_service",
    "llm",
    "rag",
    "rbac_service",
    "user_service",
    "web_page",
    "web_search",
]
