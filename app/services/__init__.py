"""服务层：路由只管 HTTP，业务与外部 IO 全在这层。

按字母序（与各模块自己的 docstring 一对一）：

- ``admin_service``  — 控制台账号 / 会话 / 角色的认证仓库
- ``analytics``      — 数据分析（Text-to-SQL 智能问数，只读）
- ``blog``           — 个人博客：文章 CRUD、发布同步知识库与前台 AI 检索
- ``chat_history``   — AI 对话会话 / 消息持久化（仅本人可见）
- ``chat_tools``     — 给对话模型挂的联网工具与「生成→工具→再生成」循环
- ``datasource``     — 外部数据源登记 / 掩码 / 连通性测试（问数可选目标库）
- ``document_parser``— 录入前的文件解析：各种格式统一成「文本 + 内嵌图片」
- ``item_service``   — items 表的异步仓库（示例业务数据）
- ``llm``            — LLM 网关：provider 注册表（OpenAI 兼容系与 mock）
- ``markdown_text``  — 录入正文的格式归一化：HTML 富文本压成 Markdown 源码
- ``provider_store`` — 模型提供者配置的运行时缓存（.env 兜底）
- ``rag``            — 检索增强生成：切片、入库、混合检索、给答案
- ``rag_queue``      — 录入 / 重建索引的后台任务队列（不阻塞 HTTP 请求）
- ``rbac_service``   — 后台角色 / 账号 / 权限解析（RBAC）
- ``reasoning``      — 把「思考过程」从模型正文里分出来的状态机
- ``reranker``       — cross-encoder 精排客户端（宽召回后的二次打分）
- ``system_config``  — 系统配置（各功能选用哪个 provider）读写
- ``user_service``   — users 表的异步仓库（示例业务数据，不是登录账号）
- ``web_page``       — 抓网页并缩减成可入库的正文
- ``web_search``     — 联网检索的搜索源适配层（tavily / serper / bing）

``rag_queue`` / ``reasoning`` / ``reranker`` 只被同层模块间接引用，
所以不在下面的导入清单与 ``__all__`` 里。
"""

from . import (
    admin_service,
    analytics,
    blog,
    blog_feed,
    chat_history,
    chat_tools,
    datasource,
    document_parser,
    item_service,
    llm,
    markdown_text,
    provider_store,
    rag,
    rbac_service,
    system_config,
    user_service,
    web_page,
    web_search,
)

__all__ = [
    "admin_service",
    "analytics",
    "blog",
    "blog_feed",
    "chat_history",
    "chat_tools",
    "datasource",
    "document_parser",
    "item_service",
    "llm",
    "markdown_text",
    "provider_store",
    "rag",
    "rbac_service",
    "system_config",
    "user_service",
    "web_page",
    "web_search",
]
