<script setup lang="ts">
/**
 * 智能问数（Text-to-SQL）—— 对话式问数流。
 *   问题 → 大模型生成只读 SQL → 后端强校验并执行 → 结果再交给大模型出结论。
 * 布局：左侧数据源 + 可搜索表结构；右侧会话流（历史由后端持久化，可回放/删除）。
 * 安全：仅 SELECT/WITH、单语句、只读事务兜底；凭证列已被后端屏蔽。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { message } from 'ant-design-vue';
import { analyticsApi } from '@/api';
import RichText from '@/components/RichText.vue';
import { useBusinessStore } from '@/store/modules/business';
import type { AnalyticsQueryResult, AnalyticsSource, AnalyticsTable } from '@/typings';

/** 会话流里的一条问答（pending=正在分析，failMsg=请求本身失败） */
interface ChatItem {
  key: string;
  question: string;
  source: string;
  createdAt: number;
  result: AnalyticsQueryResult | null;
  pending?: boolean;
  dry?: boolean;
  failMsg?: string;
}

const business = useBusinessStore();
const backend = computed<any>(() => business.activeProvider);

/* ---------------- 数据源 ---------------- */
const sources = ref<AnalyticsSource[]>([]);
const source = ref('main');
const sourceOptions = computed(() =>
  sources.value.map((s) => ({
    value: s.key,
    label: s.builtin ? `${s.name}（内置）` : `${s.name}·${s.dialect}`,
  })),
);
const currentSource = computed(() => sources.value.find((s) => s.key === source.value));
function sourceName(key: string): string {
  return sources.value.find((s) => s.key === key)?.name || key;
}

/* ---------------- 表结构浏览器（可搜索） ---------------- */
const schema = ref<AnalyticsTable[]>([]);
const schemaLoading = ref(false);
const schemaKeyword = ref('');
const expanded = ref<Set<string>>(new Set());

// 大库（几百张表）只渲染过滤后的前 80 条，避免整页折叠面板翻不动
const filteredSchema = computed(() => {
  const kw = schemaKeyword.value.trim().toLowerCase();
  const list = kw
    ? schema.value.filter(
        (t) =>
          t.name.toLowerCase().includes(kw) ||
          (t.comment || '').toLowerCase().includes(kw) ||
          t.columns.some((c) => c.name.toLowerCase().includes(kw)),
      )
    : schema.value;
  return list.slice(0, 80);
});

function toggleTable(name: string): void {
  const next = new Set(expanded.value);
  if (next.has(name)) {
    next.delete(name);
  } else {
    next.add(name);
  }
  expanded.value = next;
}

function quoteTable(t: AnalyticsTable): void {
  const text = t.comment ? `${t.name}（${t.comment}）` : t.name;
  question.value = (question.value.trim() + ' ' + text + ' ').trimStart();
}

/* ---------------- 会话流 ---------------- */
const question = ref('');
const dryRun = ref(false);
const loading = ref(false);
const messages = ref<ChatItem[]>([]);
const listRef = ref<HTMLElement | null>(null);

// 示例问题：主库用固定业务示例；外部源根据表注释动态生成，避免驴唇不对马嘴
const samples = computed(() => {
  if (source.value === 'main') {
    return [
      '一共有多少个用户？',
      '最近创建的商品有哪些？列出标题和价格',
      '每个用户的商品数量分别是多少？',
      '知识库中共有多少篇文档、多少个分片？',
    ];
  }
  return schema.value
    .filter((t) => t.comment)
    .slice(0, 3)
    .map((t) => `帮我看看「${t.comment}」(${t.name}) 这张表的数据情况`);
});

function scrollToBottom(): void {
  nextTick(() => {
    const el = listRef.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
}
watch(() => messages.value.length, scrollToBottom);
watch(loading, (v) => { if (!v) scrollToBottom(); });

async function loadSources(): Promise<void> {
  try {
    const data = await analyticsApi.sources();
    sources.value = data ?? [];
    if (!sources.value.some((s) => s.key === source.value)) {
      source.value = 'main';
    }
  } catch (e: any) {
    message.error(e?.detail || '读取数据源列表失败');
  }
}

async function loadSchema(): Promise<void> {
  schemaLoading.value = true;
  try {
    const data = await analyticsApi.schema(source.value);
    schema.value = data?.tables ?? [];
    expanded.value = new Set();
  } catch (e: any) {
    schema.value = [];
    message.error(e?.detail || '读取表结构失败');
  } finally {
    schemaLoading.value = false;
  }
}

async function loadHistory(): Promise<void> {
  try {
    const data = await analyticsApi.history();
    messages.value = (data?.messages ?? []).map((m) => ({
      key: m.id,
      question: m.question,
      source: m.source,
      createdAt: m.created_at,
      result: m.result,
    }));
    scrollToBottom();
  } catch {
    // 历史拉不到不弹错，页面还能正常问
    messages.value = [];
  }
}

function onSourceChange(): void {
  // 会话流跨源保留（每条消息带来源标签），只刷新左侧表结构
  loadSchema();
}

async function onAsk(): Promise<void> {
  const q = question.value.trim();
  if (!q) return message.warning('请先输入你的问题');
  if (loading.value) return;
  loading.value = true;
  const item: ChatItem = {
    key: `local-${Date.now()}`,
    question: q,
    source: source.value,
    createdAt: Date.now() / 1000,
    result: null,
    pending: true,
    dry: dryRun.value,
  };
  messages.value.push(item);
  question.value = '';
  scrollToBottom();
  try {
    const r = await analyticsApi.query({
      question: q,
      source: item.source,
      dry_run: item.dry,
    });
    item.result = r;
    item.pending = false;
    if (r.chat_id) item.key = r.chat_id;
    if (r.error) message.warning('本次未成功，见对话卡片提示');
  } catch (e: any) {
    item.pending = false;
    item.failMsg = e?.detail || '问数请求失败，请稍后重试';
  } finally {
    loading.value = false;
    scrollToBottom();
  }
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    onAsk();
  }
}

async function onRemove(item: ChatItem): Promise<void> {
  messages.value = messages.value.filter((m) => m !== item);
  if (item.key.startsWith('local-')) return;
  try {
    await analyticsApi.removeChat(item.key);
  } catch {
    message.error('删除失败');
  }
}

async function onClearAll(): Promise<void> {
  messages.value = [];
  try {
    await analyticsApi.clearHistory();
    message.success('已清空问数历史');
  } catch {
    message.error('清空失败');
  }
}

async function copySql(sql: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(sql);
    message.success('SQL 已复制');
  } catch {
    message.error('复制失败，请手动选取');
  }
}

/* ---------------- 结果表 ---------------- */
// 列名可能重复，用列下标做 dataIndex；行数据本身就是数组
function colsOf(r: AnalyticsQueryResult): any[] {
  return r.columns.map((c, i) => ({
    title: c,
    dataIndex: i,
    key: `c${i}`,
    ellipsis: true,
  }));
}

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

onMounted(() => {
  business.loadProviders();
  business.pollHealth();
  loadSources().then(loadSchema);
  loadHistory();
});
</script>

<template>
  <div class="analytics-page">
    <!-- 左：数据源 + 表结构浏览器 -->
    <aside class="side">
      <div class="side-block">
        <div class="side-title">数据源</div>
        <a-select
          v-model:value="source"
          :options="sourceOptions"
          style="width: 100%"
          @change="onSourceChange"
        />
      </div>
      <div class="side-block side-schema">
        <div class="side-title-row">
          <span class="side-title">表结构（{{ schema.length }}）</span>
          <a-button type="link" size="small" :loading="schemaLoading" @click="loadSchema">
            刷新
          </a-button>
        </div>
        <a-input
          v-model:value="schemaKeyword"
          placeholder="搜索表名 / 列名 / 注释"
          size="small"
          allow-clear
        />
        <div class="schema-list">
          <div v-for="t in filteredSchema" :key="t.name" class="schema-item">
            <div class="schema-row" @click="toggleTable(t.name)">
              <span class="t-name">{{ t.name }}</span>
              <span class="t-comment" :title="t.comment">{{ t.comment }}</span>
              <span class="t-count">{{ t.columns.length }} 列</span>
            </div>
            <div v-if="expanded.has(t.name)" class="t-cols">
              <a-tag v-for="c in t.columns" :key="c.name" class="col-tag" :title="c.comment">
                {{ c.name }}<span class="col-type"> {{ c.type }}</span>
              </a-tag>
              <a class="quote-link" @click.stop="quoteTable(t)">引用到提问 →</a>
            </div>
          </div>
          <a-empty v-if="!filteredSchema.length" :image-style="{ height: '40px' }" description="没有匹配" />
        </div>
      </div>
    </aside>

    <!-- 右：对话式问数流 -->
    <section class="main">
      <header class="top-bar">
        <div class="top-title">
          智能问数
          <span class="muted">
            当前：{{ currentSource?.name || '本应用主库' }} · 模型：{{ backend?.name || '—' }}
            {{ backend?.chat_model || '默认' }} · 全程只读
          </span>
        </div>
        <a-popconfirm title="清空全部历史问答？不可恢复" @confirm="onClearAll">
          <a-button size="small" danger :disabled="!messages.length">清空会话</a-button>
        </a-popconfirm>
      </header>

      <a-alert
        v-if="!business.backendOk"
        type="warning"
        show-icon
        banner
        message="后端当前未响应（/health），问数可能失败"
      />

      <div ref="listRef" class="chat-list">
        <!-- 空状态：引导 + 示例问题 -->
        <div v-if="!messages.length" class="hero">
          <div class="hero-title">试着问我一个数据问题</div>
          <div class="hero-sub">
            自动生成只读 SQL 并执行，凭证字段不会出现；分析历史自动保存，可随时回看或删除
          </div>
          <div class="samples">
            <a-tag v-for="s in samples" :key="s" color="blue" class="sample-tag" @click="question = s">
              {{ s }}
            </a-tag>
          </div>
        </div>

        <template v-for="m in messages" :key="m.key">
          <!-- 用户问题 -->
          <div class="msg-user">
            <div class="bubble">
              <div class="bubble-q">{{ m.question }}</div>
              <div class="q-meta">{{ sourceName(m.source) }} · {{ fmtTime(m.createdAt) }}</div>
            </div>
          </div>

          <!-- AI 回答卡片 -->
          <div class="msg-ai">
            <div class="ai-card">
              <div v-if="m.pending" class="pending">
                <a-spin size="small" />
                <span>正在分析：理解问题 → 定位表 → 生成并校验 SQL → 只读执行 → 总结…</span>
              </div>
              <template v-else>
                <a-alert v-if="m.failMsg" type="error" show-icon :message="m.failMsg" />
                <a-alert
                  v-else-if="m.result?.error"
                  type="error"
                  show-icon
                  :message="m.result.error"
                />

                <div v-if="m.result?.sql" class="sql-wrap">
                  <pre class="sql-box">{{ m.result.sql }}</pre>
                  <a-button size="small" class="copy-btn" @click="copySql(m.result.sql)">
                    复制 SQL
                  </a-button>
                </div>
                <div v-if="m.result?.brief" class="brief">{{ m.result.brief }}</div>

                <div v-if="m.result && m.result.columns.length" class="data-wrap">
                  <a-table
                    :data-source="m.result.rows"
                    :columns="colsOf(m.result)"
                    :pagination="{ pageSize: 10, size: 'small', hideOnSinglePage: true }"
                    :scroll="{ x: 'max-content' }"
                    size="small"
                    row-key="0"
                  />
                </div>

                <div v-if="m.result?.conclusion" class="conclusion">
                  <RichText :text="m.result.conclusion" />
                </div>
                <div v-else-if="m.dry && m.result && !m.result.error" class="muted dry-tip">
                  已按「只生成 SQL」模式返回，未执行查询
                </div>

                <div class="ai-meta">
                  <span class="muted">
                    <template v-if="m.result && !m.result.error">
                      {{ m.result.row_count }} 行<template v-if="m.result.truncated">（已截断）</template>
                      <template v-if="m.result.dialect"> · {{ m.result.dialect }}</template>
                    </template>
                    <template v-else>未成功</template>
                  </span>
                  <a-popconfirm title="删除这条问答记录？" @confirm="onRemove(m)">
                    <a class="del-link">删除</a>
                  </a-popconfirm>
                </div>
              </template>
            </div>
          </div>
        </template>
      </div>

      <!-- 底部输入区 -->
      <footer class="composer">
        <a-textarea
          v-model:value="question"
          :auto-size="{ minRows: 2, maxRows: 5 }"
          placeholder="继续追问…（Enter 发送，Shift+Enter 换行）"
          :maxlength="2000"
          :disabled="loading"
          @keydown="onKeydown"
        />
        <div class="composer-bar">
          <a-checkbox v-model:checked="dryRun" :disabled="loading">只生成 SQL，不执行</a-checkbox>
          <a-space>
            <a-button v-if="!loading" size="small" :disabled="!question.trim()" @click="question = ''">
              清空输入
            </a-button>
            <a-button type="primary" :loading="loading" @click="onAsk">
              {{ dryRun ? '生成 SQL' : '开始分析' }}
            </a-button>
          </a-space>
        </div>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.analytics-page {
  display: flex;
  gap: 16px;
  /* 高度单点：同对话页，跟随全局留白变量，避免窄屏高度下出现双滚动条 */
  height: var(--page-h);
  min-height: 480px;
}

/* ---------- 左侧栏 ---------- */
.side {
  width: 300px;
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--app-panel);
  border-radius: 8px;
  padding: 14px;
  overflow: hidden;
}
/* 笔记本：表结构列表靠 ellipsis 展示，压窄到 244 把宽度让给右侧结果区 */
@media (max-width: 1440px) {
  .side { width: 244px; padding: 12px; }
}
.side-schema {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.side-title {
  font-weight: 600;
  color: var(--app-fg);
  font-size: 13px;
}
.side-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.schema-list {
  flex: 1;
  overflow: auto;
  margin-top: 8px;
}
.schema-item + .schema-item {
  border-top: 1px solid var(--app-border);
}
.schema-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 6px 2px;
  cursor: pointer;
  min-width: 0;
}
.schema-row:hover {
  background: var(--app-panel-2);
}
.t-name {
  font-family: 'SF Mono', Consolas, Menlo, monospace;
  font-size: 12px;
  color: var(--app-fg);
  flex: none;
  max-width: 55%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.t-comment {
  color: var(--app-muted);
  font-size: 12px;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.t-count {
  color: var(--app-muted);
  font-size: 11px;
  flex: none;
}
.t-cols {
  padding: 4px 2px 8px 8px;
}
.col-tag {
  margin: 2px 4px 2px 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.col-type {
  color: var(--app-muted);
  font-size: 11px;
}
.quote-link {
  display: inline-block;
  margin-top: 4px;
  font-size: 12px;
}

/* ---------- 右侧主区 ---------- */
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--app-panel);
  border-radius: 8px;
  padding: 14px 16px;
}
.top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 8px;
}
.top-title {
  font-weight: 600;
  font-size: 15px;
  color: var(--app-fg);
  min-width: 0;
}
.muted {
  color: var(--app-muted);
  font-weight: 400;
  font-size: 12px;
}
.chat-list {
  flex: 1;
  overflow: auto;
  padding: 8px 4px;
}

.hero {
  text-align: center;
  padding: 48px 16px;
}
.hero-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--app-fg);
}
.hero-sub {
  color: var(--app-muted);
  font-size: 13px;
  margin: 8px auto 16px;
  max-width: 520px;
}
.samples {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
}
.sample-tag {
  cursor: pointer;
  margin: 0;
  max-width: 100%;
}

.msg-user {
  display: flex;
  justify-content: flex-end;
  margin: 14px 0 8px;
}
.bubble {
  background: #1677ff;
  color: #fff;
  border-radius: 10px 10px 2px 10px;
  padding: 8px 12px;
  max-width: 72%;
}
.bubble-q {
  white-space: pre-wrap;
  word-break: break-word;
}
.q-meta {
  opacity: 0.75;
  font-size: 11px;
  margin-top: 4px;
  text-align: right;
}

.msg-ai {
  display: flex;
  justify-content: flex-start;
  margin-bottom: 14px;
}
.ai-card {
  background: var(--app-panel-2);
  border: 1px solid var(--app-border);
  border-radius: 10px 10px 10px 2px;
  padding: 12px;
  max-width: 92%;
  min-width: 240px;
}
.pending {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--app-muted);
  font-size: 13px;
}
.sql-wrap {
  position: relative;
  margin: 8px 0 4px;
}
.sql-box {
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 8px;
  padding: 10px 12px;
  font-family: 'SF Mono', Consolas, Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  max-height: 240px;
  overflow: auto;
}
.copy-btn {
  position: absolute;
  top: 6px;
  right: 6px;
}
.brief {
  color: var(--app-muted);
  font-size: 13px;
  margin: 4px 0 8px;
}
.data-wrap {
  margin: 8px 0;
}
.conclusion {
  background: var(--app-panel);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-top: 8px;
}
.dry-tip {
  margin-top: 6px;
}
.ai-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 8px;
}
.del-link {
  color: var(--app-muted);
  font-size: 12px;
}
.del-link:hover {
  color: #ef4444;
}

.composer {
  border-top: 1px solid var(--app-border);
  padding-top: 10px;
}
.composer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  margin-top: 8px;
  gap: 8px;
}

/* 窄屏：隐藏侧栏，专注问答 */
@media (max-width: 992px) {
  .side {
    display: none;
  }
}
</style>
