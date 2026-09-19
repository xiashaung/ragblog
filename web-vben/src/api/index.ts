/**
 * 业务 API —— 所有后端接口调用集中在此，页面只引用本文件。
 * 前缀管理：admin 体系走 /admin（见后端 app/main.py 的 admin.router）；
 * 其余接口（health/ai/knowledge）统一走 /api/v1。
 */
import http, {adminRequest, request, httpAdmin} from './request';
import type {
    AccountItem,
    AnalyticsHistoryResponse,
    AnalyticsQueryResult,
    AnalyticsSchema,
    AnalyticsSource,
    ChatAppendPayload,
    ChatAppendResult,
    ChatMessageListResult,
    ChatSession,
    ChatSessionListResult,
    ChatStreamChunk,
    DataSourceItem,
    DataSourcePayload,
    DataSourceTestResult,
    KnowledgeDetail,
    KnowledgeDocItem,
    KnowledgeFetchResult,
    KnowledgeIngestResult,
    KnowledgeListResult,
    KnowledgePair,
    KnowledgeReindexResult,
    KnowledgeStats,
    PermissionDef,
    RoleItem,
    SessionItem,
    UploadBatchResult,
    WebMode,
} from '@/typings';

/* ---------------- 管理后台 admin 体系（Bearer token）---------------- */
// 全部改用 httpAdmin（前缀 /admin），避免与 /api/v1 前缀冲突导致 405
export const adminApi = {
    // 首次引导：创建超级管理员（库内已有账号会 409）。
    // bootstrapKey 必须等于后端 APP_ADMIN_API_KEY，只用于这一次调用；
    // 返回体不含 token，前端需要再走一次 login。
    onboard: (data: { username: string; password: string; name: string }, bootstrapKey?: string) =>
        httpAdmin.post(`/admin/onboard`, data, {
            headers: bootstrapKey ? {'X-API-Key': bootstrapKey} : undefined,
        }),
    // 登录：返回 token + admin（本地存 admin_token / user_info）
    login: (data: { username: string; password: string }) =>
        httpAdmin.post(`/admin/login`, data),
    // 刷新 token
    refresh: () => httpAdmin.post(`/admin/refresh`),
    // 当前登录身份 + 角色 + 权限（菜单/按钮据此渲染）
    me: () => adminRequest<{
        id: string;
        username: string;
        name: string;
        roles: string[];
        permissions: string[];
        is_superadmin: boolean;
        live_sessions: number
    }>({url: '/admin/me', method: 'get'}),
    // 在线会话列表（需 session:read）
    sessions: () => adminRequest<SessionItem[]>({url: '/admin/sessions', method: 'get'}),
    // 强制下线某个会话（需 session:revoke）
    revokeSession: (id: string) =>
        adminRequest<{ revoked: boolean }>({url: `/admin/sessions/${id}`, method: 'delete'}),
    // 登出
    logout: () => httpAdmin.post(`/admin/logout`),
};

/* ---------------- RBAC：权限目录 / 角色 / 账号 ---------------- */
// 所需权限见后端 app/rbac.py：role:read|role:manage / account:read|account:manage
export const rbacApi = {
    permissions: () => adminRequest<PermissionDef[]>({url: '/admin/permissions', method: 'get'}),
    roles: () => adminRequest<RoleItem[]>({url: '/admin/roles', method: 'get'}),
    createRole: (data: { code: string; name: string; description?: string; permissions: string[] }) =>
        adminRequest<RoleItem>({url: '/admin/roles', method: 'post', data}),
    updateRole: (
        id: string,
        data: { name?: string; description?: string; permissions?: string[] },
    ) => adminRequest<RoleItem>({url: `/admin/roles/${id}`, method: 'patch', data}),
    removeRole: (id: string) => adminRequest<{ deleted: boolean }>({url: `/admin/roles/${id}`, method: 'delete'}),
    accounts: () => adminRequest<AccountItem[]>({url: '/admin/accounts', method: 'get'}),
    createAccount: (data: {
        username: string;
        password: string;
        name?: string;
        roles: string[];
    }) => adminRequest<AccountItem>({url: '/admin/accounts', method: 'post', data}),
    updateAccount: (
        id: string,
        data: { name?: string; is_active?: boolean; password?: string },
    ) => adminRequest<AccountItem>({url: `/admin/accounts/${id}`, method: 'patch', data}),
    setAccountRoles: (id: string, roles: string[]) =>
        adminRequest<AccountItem>({url: `/admin/accounts/${id}/roles`, method: 'put', data: {roles}}),
    removeAccount: (id: string) =>
        adminRequest<{ deleted: boolean }>({url: `/admin/accounts/${id}`, method: 'delete'}),
};

/* ---------------- OpenAI 兼容 / AI 接口 ---------------- */
export const aiApi = {
    providers: () => request<any[]>({url: '/ai/providers', method: 'get'}),
    chat: (data: any) => http.post('/ai/chat', data), // 流式走 fetch，非流式走这里
    health: () => request<any>({url: '/health', method: 'get'}),
};

/* ---------------- 嵌入向量 ---------------- */
// provider / model 不再由前端传递，后端按 APP_LLM_DEFAULT_PROVIDER 配置调用
export const embedApi = {
    run: (data: { input: string[] }) =>
        request<any>({url: '/ai/embeddings', method: 'post', data}),
};

/* ---------------- 知识库 RAG 管理 ---------------- */
// provider / model 不再由前端传递，后端按 APP_LLM_DEFAULT_PROVIDER 配置调用
// 入库 / 重建 / 编辑正文都要重新调用嵌入模型，耗时与文档长度成正比，单独放宽超时
const LLM_TIMEOUT = 5 * 60 * 1000;

export const ragApi = {
    list: (params?: {
        keyword?: string;
        provider?: string;
        only_stale?: boolean;
        offset?: number;
        limit?: number;
    }) => request<KnowledgeListResult>({url: '/ai/knowledge', method: 'get', params}),
    stats: () => request<KnowledgeStats>({url: '/ai/knowledge/stats', method: 'get'}),
    detail: (id: string, chunkLimit?: number) =>
        request<KnowledgeDetail>({
            url: `/ai/knowledge/${id}`,
            method: 'get',
            params: chunkLimit ? {chunk_limit: chunkLimit} : undefined,
        }),
    ingest: (data: { title: string; content: string; chunk_size?: number }) =>
        request<KnowledgeIngestResult>({
            url: '/ai/knowledge/ingest',
            method: 'post',
            data,
            timeout: LLM_TIMEOUT,
        }),
    // 一批问答对合为一篇 FAQ 文档
    ingestPairs: (data: {
        title: string;
        pairs: KnowledgePair[];
        chunk_size?: number;
    }) =>
        request<KnowledgeIngestResult>({
            url: '/ai/knowledge/ingest-pairs',
            method: 'post',
            data,
            timeout: LLM_TIMEOUT,
        }),
    // 后端抓取网页正文后入库（标题默认取页面 <title>）
    fetchUrl: (data: {
        url: string;
        title?: string;
        chunk_size?: number;
        max_chars?: number;
    }) =>
        request<KnowledgeFetchResult>({
            url: '/ai/knowledge/fetch-url',
            method: 'post',
            data,
            timeout: LLM_TIMEOUT,
        }),
    // 改名（廉价）或替换正文（重新分片 + 重新向量化）
    update: (id: string, data: { title?: string; content?: string }) =>
        request<KnowledgeDocItem>({
            url: `/ai/knowledge/${id}`,
            method: 'patch',
            data,
            timeout: LLM_TIMEOUT,
        }),
    reindex: (id: string) =>
        request<KnowledgeReindexResult>({
            url: `/ai/knowledge/${id}/reindex`,
            method: 'post',
            timeout: LLM_TIMEOUT,
        }),
    // 更换向量模型后重建索引
    replaceVectors: (id: string, provider: string) =>
        request<KnowledgeReindexResult>({
            url: `/ai/knowledge/${id}/replace-vectors`,
            method: 'post',
            data: {provider},
            timeout: LLM_TIMEOUT,
        }),
    search: (data: { query: string; top_k: number }) =>
        request<any>({url: '/ai/knowledge/search', method: 'post', data}),
    // images：可选的多模态图片（http(s) 或 base64 data URL），仅在对话后端为视觉模型时生效
    answer: (data: { query: string; top_k: number; images?: string[] }) =>
        request<any>({url: '/ai/knowledge/answer', method: 'post', data}),
    /** 流式问答 —— 后端逐行产出 NDJSON，只有原生 fetch 能拿到 ReadableStream */
    answerStream: (
        data: { query: string; top_k: number; images?: string[]; stream?: boolean },
        signal?: AbortSignal,
    ): Promise<Response> => {
        const token = localStorage.getItem('admin_token') || '';
        return fetch('/api/v1/ai/knowledge/answer', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                ...(token ? {Authorization: `Bearer ${token}`} : {}),
            },
            body: JSON.stringify({...data, stream: true}),
            signal,
        });
    },
    remove: (id: string) => request<any>({url: `/ai/knowledge/${id}`, method: 'delete'}),
    batchRemove: (ids: string[]) =>
        request<{ deleted: number; failed: string[] }>({
            url: '/ai/knowledge/batch-delete',
            method: 'post',
            data: {ids},
        }),
};

/* ---------------- 数据分析（Text-to-SQL 智能问数）---------------- */
// 接口需 analytics:read 权限；Bearer 由 request 拦截器自动携带。
// 一次问数会走“生成 SQL + 总结结论”两次大模型调用，耗时与结果行数成正比，放宽超时。
export const analyticsApi = {
    // 可选数据源（内置主库 + 已启用的外部源）
    sources: () => request<AnalyticsSource[]>({url: '/analytics/sources', method: 'get'}),
    schema: (source = 'main') =>
        request<AnalyticsSchema>({
            url: '/analytics/schema',
            method: 'get',
            params: {source},
        }),
    query: (data: { question: string; source?: string; dry_run?: boolean }) =>
        request<AnalyticsQueryResult>({
            url: '/analytics/query',
            method: 'post',
            data,
            timeout: LLM_TIMEOUT,
        }),
    // 问数历史（仅本人）：回放 / 删单条 / 清空
    history: (limit = 100) =>
        request<AnalyticsHistoryResponse>({
            url: '/analytics/history',
            method: 'get',
            params: {limit},
        }),
    removeChat: (id: string) =>
        request<void>({url: `/analytics/history/${id}`, method: 'delete'}),
    clearHistory: () => request<void>({url: '/analytics/history', method: 'delete'}),
};

/* ---------------- 外部数据源管理（智能问数多库）---------------- */
// 读需 datasource:read；写/删/测试需 datasource:manage（默认只给超管）。
export const datasourceApi = {
    list: () => adminRequest<DataSourceItem[]>({url: '/admin/data-sources', method: 'get'}),
    create: (data: DataSourcePayload) =>
        adminRequest<DataSourceItem>({url: '/admin/data-sources', method: 'post', data}),
    update: (id: string, data: Partial<DataSourcePayload>) =>
        adminRequest<DataSourceItem>({
            url: `/admin/data-sources/${id}`,
            method: 'patch',
            data,
        }),
    remove: (id: string) =>
        adminRequest<{ deleted: boolean }>({url: `/admin/data-sources/${id}`, method: 'delete'}),
    test: (data: { dialect: string; url: string; existing_id?: string }) =>
        adminRequest<DataSourceTestResult>({
            url: '/admin/data-sources/test',
            method: 'post',
            data,
            timeout: 30 * 1000,
        }),
};

/* ---------------- 文件上传入库 ---------------- */
export const uploadApi = {
    uploadFile: (file: File, title: string, chunkSize?: number) => {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('title', title || file.name.split('.')[0]);
        if (chunkSize) {
            formData.append('chunk_size', String(chunkSize));
        }
        // 走同一个 axios 实例：拦截器已把 envelope.data 平移到 res.data
        return http.post('/upload', formData, {
            headers: {'Content-Type': 'multipart/form-data'},
            timeout: LLM_TIMEOUT,
        });
    },
    // 批量：标题取文件名主干（同名覆盖），一次最多 20 个，逐文件返回成败
    uploadBatch: (files: File[], chunkSize?: number) => {
        const formData = new FormData();
        files.forEach((f) => formData.append('files', f));
        if (chunkSize) {
            formData.append('chunk_size', String(chunkSize));
        }
        return request<UploadBatchResult>({
            url: '/upload/batch',
            method: 'post',
            data: formData,
            headers: {'Content-Type': 'multipart/form-data'},
            timeout: LLM_TIMEOUT,
        });
    },
};

/* ---------------- AI 对话（会话持久化 + 流式） ---------------- */
// provider / model 由后端 .env 决定，前端不传；会话按登录身份隔离，只有本人可见。
export const chatApi = {
    sessions: (limit = 50) =>
        request<ChatSessionListResult>({
            url: '/ai/chat/sessions',
            method: 'get',
            params: {limit},
        }),
    // 标题留空用占位名，首条提问入库后后端自动改名
    create: (title = '') =>
        request<ChatSession>({url: '/ai/chat/sessions', method: 'post', data: {title}}),
    rename: (id: string, title: string) =>
        request<ChatSession>({url: `/ai/chat/sessions/${id}`, method: 'patch', data: {title}}),
    remove: (id: string) =>
        request<void>({url: `/ai/chat/sessions/${id}`, method: 'delete'}),
    messages: (id: string) =>
        request<ChatMessageListResult>({url: `/ai/chat/sessions/${id}/messages`, method: 'get'}),
    /** 一轮生成完成后整轮入库（失败的半截回答不存，避免回放混入噪声） */
    append: (id: string, data: ChatAppendPayload) =>
        request<ChatAppendResult>({
            url: `/ai/chat/sessions/${id}/messages`,
            method: 'post',
            data,
            timeout: LLM_TIMEOUT,
        }),
};

/**
 * 流式对话 —— 后端逐行产出 NDJSON，只有原生 fetch 能拿到 ReadableStream。
 *
 * 逐行解析时必须自己缓存半个行：一个网络包可能把一条 JSON 拆成两段，
 * 直接对每包 split 后逐行 parse 会把被拆开的片段当坏行丢掉（旧实现就这么丢字）。
 * `signal` 用于「停止生成」；上游报错以最后一块的 `error` 字段透出（流已开就改不了状态码）。
 */
export async function streamChat(
    body: {
        messages: unknown[];
        temperature: number;
        max_tokens: number;
        response_format: Record<string, string> | null;
        /** 联网开关：不传后端按 auto（模型自己判断要不要查） */
        web_mode?: WebMode;
    },
    onChunk: (chunk: ChatStreamChunk) => void,
    signal?: AbortSignal,
): Promise<void> {
    const token = localStorage.getItem('admin_token') || '';
    const res = await fetch('/api/v1/ai/chat', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...(token ? {Authorization: `Bearer ${token}`} : {}),
        },
        body: JSON.stringify({...body, stream: true}),
        signal,
    });
    if (!res.ok) {
        const err = await res.json().catch(() => null);
        throw new Error(err?.message || `后端返回 ${res.status}`);
    }
    if (!res.body) throw new Error('当前浏览器不支持流式读取');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const feed = (line: string): void => {
        const raw = line.startsWith('data:') ? line.slice(5).trim() : line.trim();
        if (!raw) return;
        try {
            onChunk(JSON.parse(raw) as ChatStreamChunk);
        } catch {
            /* 单行解析不了就跳过，不让它打断整段输出 */
        }
    };

    while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() ?? ''; // 最后一段可能是半行，留给下一个包拼上
        lines.forEach(feed);
    }
    buffer += decoder.decode();
    feed(buffer);
}
