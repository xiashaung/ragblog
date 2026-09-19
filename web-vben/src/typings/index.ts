/** 全局类型声明。 */

/** 后台账号（登录 / /admin/me 返回的身份） */
export interface AdminUser {
  id: string;
  username: string;
  name: string;
  is_active: boolean;
  /** RBAC：生效角色 code 与合并后的权限码（``['*']`` 表示超管全权） */
  roles?: string[];
  permissions?: string[];
}

/** 权限码目录项（后端 app/rbac.py 定义，前端只读渲染） */
export interface PermissionDef {
  code: string;
  label: string;
  group: string;
  description?: string;
}

/** 角色（可在后台增删改） */
export interface RoleItem {
  id: string;
  code: string;
  name: string;
  description: string;
  permissions: string[];
  is_builtin: boolean;
  members: number;
}

/** 后台账号列表项 */
export interface AccountItem {
  id: string;
  username: string;
  name: string;
  is_active: boolean;
  created_at: number;
  roles: string[];
}

/** 在线会话 */
export interface SessionItem {
  id: string;
  username: string;
  admin_id: string;
  expires_at: number;
}

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface KnowledgeDocItem {
  knowledge_id: string;
  title: string;
  provider: string;
  chunks: number;
  chars: number;
  preview: string;
  /** 旧数据未保存原文时为 false，不能编辑 / 重建索引 */
  has_content: boolean;
  source_file?: string | null;
  /** 录入方式：file / text / faq / url */
  kind: 'file' | 'text' | 'faq' | 'url';
  source_url?: string | null;
  metadata?: Record<string, any> | null;
  ingested_at: number;
  updated_at: number;
  /** 向量由非当前默认 provider 生成，检索不到，需重建 */
  stale: boolean;
}

export interface KnowledgeListResult {
  total: number;
  offset: number;
  limit: number;
  default_provider: string;
  items: KnowledgeDocItem[];
}

export interface KnowledgeChunkItem {
  id: string;
  index: number;
  source: string;
  text: string;
  dim: number;
}

export interface KnowledgeDetail {
  doc: KnowledgeDocItem;
  content: string | null;
  chunk_total: number;
  chunks: KnowledgeChunkItem[];
}

export interface KnowledgeIngestResult {
  knowledge_id: string;
  provider: string;
  chunks: number;
  chars: number;
}

export interface KnowledgeReindexResult {
  knowledge_id: string;
  provider: string;
  previous_provider: string | null;
  chunks: number;
  chars: number;
}

/** 问答对录入的入参 */
export interface KnowledgePair {
  question: string;
  answer: string;
}

/** URL 录入：在入库结果上多一个页面信息 */
export interface KnowledgeFetchResult extends KnowledgeIngestResult {
  title: string;
  url: string;
  final_url?: string | null;
  page_chars: number;
  truncated: boolean;
}

export interface KnowledgeStats {
  docs: number;
  chunks: number;
  chars: number;
  default_provider: string;
  embedding_model: string;
  /** 'pgvector'：距离在库内算且有 HNSW 索引；'json'：SQLite 回退方案 */
  vector_backend: string;
  vector_dim: number;
  /** 有分片但没有可用向量的行数（检索不到，需重建索引） */
  vectorless_chunks: number;
  stale_docs: number;
  missing_content_docs: number;
  latest_updated_at: number | null;
  by_provider: { provider: string; docs: number; chunks: number; stale: boolean }[];
}

export interface UploadResult {
  status: 'success' | 'error';
  knowledge_id?: string | null;
  chunks: number;
  chars?: number;
  error?: string | null;
}

export interface UploadBatchItem extends UploadResult {
  filename: string;
}

export interface UploadBatchResult {
  results: UploadBatchItem[];
  total: number;
  succeeded: number;
  failed: number;
}

export interface MenuItem {
  key: string;
  title: string;
  icon?: string;
  /** RBAC：进入该页所需的权限码（空 = 登录即可） */
  permissions?: string[];
  hide?: boolean;
}

/* ---------------- 数据分析（Text-to-SQL 智能问数）---------------- */

/** 一列的元信息（后端已剔除凭证列） */
export interface AnalyticsColumn {
  name: string;
  type: string;
  nullable: boolean;
  comment?: string;
}

/** 一张可问数的表 */
export interface AnalyticsTable {
  name: string;
  comment?: string;
  columns: AnalyticsColumn[];
}

/** GET /analytics/schema 返回的表结构快照 */
export interface AnalyticsSchema {
  dialect: string;
  database: string;
  source: string;
  tables: AnalyticsTable[];
}

/** GET /analytics/sources 返回：问数页可选的数据源 */
export interface AnalyticsSource {
  key: string;
  name: string;
  dialect: string;
  builtin: boolean;
}

/** POST /analytics/query 返回：生成的 SQL、执行结果与 AI 结论 */
export interface AnalyticsQueryResult {
  question: string;
  provider: string;
  source: string;
  dialect: string;
  sql: string;
  brief: string;
  columns: string[];
  rows: (string | number | boolean | null)[][];
  row_count: number;
  truncated: boolean;
  conclusion: string;
  error: string | null;
  usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number } | null;
  chat_id?: string;
  created_at?: number;
}

/** 一条可回放的问数历史（后端存了完整问数响应） */
export interface AnalyticsHistoryItem {
  id: string;
  question: string;
  source: string;
  created_at: number;
  result: AnalyticsQueryResult;
}

export interface AnalyticsHistoryResponse {
  messages: AnalyticsHistoryItem[];
}

/* ---------------- 外部数据源管理（智能问数多库）---------------- */

/** 一条数据源（连接串只回掩码，永不含明文口令） */
export interface DataSourceItem {
  id: string;
  name: string;
  dialect: string;
  url_masked: string;
  readonly: boolean;
  enabled: boolean;
  description: string;
  created_at: number;
  updated_at: number;
}

/** 新建 / 编辑数据源的提交体（编辑时 url 留空表示不改连接串） */
export interface DataSourcePayload {
  name: string;
  dialect: string;
  url: string;
  readonly?: boolean;
  enabled?: boolean;
  description?: string;
}

/** POST /admin/data-sources/test 返回 */
export interface DataSourceTestResult {
  ok: boolean;
  detail: string;
  table_count: number;
}

/* ---------------- AI 对话会话持久化 ---------------- */

/** 一个对话会话（后端按登录身份隔离，仅本人可见） */
export interface ChatSession {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  /** 会话内消息条数（含用户与助手两方） */
  message_count: number;
}

export interface ChatSessionListResult {
  sessions: ChatSession[];
}

/** token 用量（流式最后一块或非流式响应里带回来） */
export interface ChatUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

/** 会话里的一条消息（回放用；images 是图片 data URL） */
export interface ChatMessage {
  id: string;
  /** 会话内递增序号，排序靠它（时间戳是秒级，一轮两条会撞） */
  seq: number;
  role: 'user' | 'assistant';
  content: string;
  /** 思考过程（深度思考模型的推理内容） */
  thinking?: string;
  images: string[];
  usage: ChatUsage | null;
  /** 因体积过大而未持久化的图片张数（>0 要提示「图片未持久化」） */
  images_dropped: number;
  /** 这一轮联网命中的链接（回放时仍能看出答案是查来的、来源是哪几条） */
  web_sources?: WebSource[];
  created_at: number;
}

export interface ChatMessageListResult {
  messages: ChatMessage[];
}

/** 一轮问答生成完成后整轮入库的提交体 */
export interface ChatAppendPayload {
  messages: Array<{
    role: 'user' | 'assistant';
    content: string;
    images?: string[];
    usage?: ChatUsage | null;
    web_sources?: WebSource[];
  }>;
}

export interface ChatAppendResult {
  session: ChatSession;
  messages: ChatMessage[];
}

/** 联网命中的一条链接 */
export interface WebSource {
  title: string;
  url: string;
  snippet: string;
}

/** 联网开关：auto=模型自己判断，forced=首轮强制查，off=不挂工具 */
export type WebMode = 'auto' | 'forced' | 'off';

/** 后端透出的一次工具调用过程（call=正要查，result=查完了或失败了） */
export interface ChatToolEvent {
  phase: 'call' | 'result';
  name: 'web_search' | 'fetch_page' | string;
  round: number;
  arguments?: string | null;
  /** 服务端解析好的人话（「搜索：北京天气」），优先显示它 */
  detail?: string | null;
  ok?: boolean;
  error?: string | null;
  sources?: WebSource[];
}

/** NDJSON 流式对话的一块（后端逐行产出，最后一块带 done/usage 或 error） */
export interface ChatStreamChunk {
  content?: string;
  reasoning?: string; // 思考过程的独立字段（delta.reasoning_content / delta.reasoning）
  finish_reason?: string | null;
  usage?: ChatUsage | null;
  done?: boolean;
  error?: string | null;
  /** 联网检索过程；只在开了 web_mode 时出现 */
  tool?: ChatToolEvent | null;
}
