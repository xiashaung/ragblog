/**
 * 博客前台 API：全部走后端公开接口（/api/v1/blog/*，无鉴权）。
 *
 * 统一信封口径（与后端中间件一致）：2xx 的 **JSON 对象** 会被包成
 * {code,data,message}，裸数组原样返回——unwrap 两种形态都要能接。
 * AI 对话是 NDJSON 流，只有原生 fetch 能拿到 ReadableStream，单独走 chatStream。
 */

const BASE = '/api/v1/blog';

export interface BlogCategoryBrief {
  name: string;
  slug: string;
}

export interface PostItem {
  slug: string;
  title: string;
  summary: string;
  cover_url?: string | null;
  tags: string[];
  // 归属分类（未分类为 null）
  category: BlogCategoryBrief | null;
  featured: boolean;
  published_at?: number | null;
  view_count: number;
}

export interface PostDetail extends PostItem {
  content: string;
  updated_at: number;
}

export interface ListResult<T> {
  total: number;
  offset: number;
  limit: number;
  items: T[];
}

/** 上/下篇导航项（后端只给 slug 与标题） */
export interface NavItem {
  slug: string;
  title: string;
}

export interface Neighbors {
  prev: NavItem | null;
  next: NavItem | null;
}

export interface TagItem {
  name: string;
  count: number;
}

/** 分类导航项：只含有已发布文章的分类（后端已筛掉空分类） */
export interface CategoryItem {
  name: string;
  slug: string;
  post_count: number;
}

/** 一条引用：以前台 slug 为锚点，index 对应回答里的 [n] 角标 */
export interface BlogCitation {
  index: number;
  slug: string;
  title: string;
  score: number;
  snippet: string;
}

export interface SearchResult {
  query: string;
  total: number;
  items: BlogCitation[];
}

export interface AnswerResult {
  query: string;
  answer: string;
  citations: BlogCitation[];
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

/** NDJSON 的一行：第一行带 citations，正文增量在 content，末行 done/error */
export interface ChatChunk {
  content?: string;
  reasoning?: string;
  done?: boolean;
  error?: string | null;
  citations?: BlogCitation[];
}

function unwrap(body: unknown): unknown {
  if (body && typeof body === 'object' && 'code' in body && 'data' in body) {
    return (body as { data: unknown }).data;
  }
  return body;
}

async function get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const qs = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== '') qs.set(k, String(v));
  });
  const url = qs.toString() ? `${path}?${qs}` : path;
  const res = await fetch(url);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error((body as { message?: string })?.message || `请求失败（${res.status}）`);
  }
  return unwrap(body) as T;
}

async function post<T>(path: string, data: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error((body as { message?: string })?.message || `请求失败（${res.status}）`);
  }
  return unwrap(body) as T;
}

export const blogApi = {
  posts: (params: { tag?: string; category?: string; offset?: number; limit?: number } = {}) =>
    get<ListResult<PostItem>>(`${BASE}/posts`, params),
  post: (slug: string) => get<PostDetail>(`${BASE}/posts/${encodeURIComponent(slug)}`),
  neighbors: (slug: string) =>
    get<Neighbors>(`${BASE}/posts/${encodeURIComponent(slug)}/neighbors`),
  related: (slug: string) =>
    get<ListResult<PostItem>>(`${BASE}/posts/${encodeURIComponent(slug)}/related`),
  tags: () => get<{ items: TagItem[] }>(`${BASE}/tags`),
  categories: () => get<{ items: CategoryItem[] }>(`${BASE}/categories`),
  search: (query: string, topK = 5) =>
    post<SearchResult>(`${BASE}/search`, { query, top_k: topK }),
  answer: (query: string, topK = 5) =>
    post<AnswerResult>(`${BASE}/answer`, { query, top_k: topK }),
};

/**
 * AI 对话（流式）：把整段多轮历史发给后端，逐行回调 NDJSON。
 * 与后台 streamChat 同一套读法：按 \n 切行、半行留给下一个包，绝不按包直接渲染。
 */
export async function chatStream(
  messages: ChatMessage[],
  onChunk: (chunk: ChatChunk) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages }),
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
    if (!line.trim()) return;
    try {
      onChunk(JSON.parse(line) as ChatChunk);
    } catch {
      /* 单行解析不了就跳过，不让它打断整段输出 */
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() ?? '';
    lines.forEach(feed);
  }
  buffer += decoder.decode();
  feed(buffer);
}
