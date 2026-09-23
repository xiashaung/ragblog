<script setup lang="ts">
// AI 对话搜索页：全站只留一个输入框——提问即同时做「语义检索 + AI 流式回答」，
// 命中文章以卡片带展示在问题气泡下方，答案附引用角标；多轮追问直接继续输入。
// 多会话管理同样只存本地：左侧会话列表可新建/切换/删除，回答生成中锁定切换，
// 标题自动取首条提问；进入时 URL 带 ?q= 会自动在当前会话提出这个问题。
import { computed, nextTick, onBeforeUnmount, ref } from 'vue';
import { useRoute } from 'vue-router';
import { message } from 'ant-design-vue';
import {
  ClearOutlined,
  DeleteOutlined,
  PlusOutlined,
  RobotOutlined,
  SearchOutlined,
  SendOutlined,
  UserOutlined,
} from '@ant-design/icons-vue';
import {
  blogApi,
  chatStream,
  type BlogCitation,
  type ChatChunk,
  type ChatMessage,
} from '@/api';
import { renderMarkdown } from '@/utils/md';

interface ChatTurn extends ChatMessage {
  /** 助手回合：渲染出的 HTML、本轮引用、是否流式进行中、错误信息 */
  html?: string;
  citations?: BlogCitation[];
  streaming?: boolean;
  error?: string;
  /** 用户提问回合：并行的语义检索命中文章 */
  matches?: BlogCitation[];
  matching?: boolean;
}

/** 落本地的最小回合：只存内容/引用/命中/错误，派生态（html/streaming/matching）不进盘 */
interface StoredTurn {
  role: 'user' | 'assistant';
  content: string;
  citations?: BlogCitation[];
  matches?: BlogCitation[];
  error?: string;
}

/** 落盘的会话：标题不单独存，加载/保存时都从首条提问现推导 */
interface PersistedSession {
  id: string;
  title: string;
  updatedAt: number;
  turns: StoredTurn[];
}

/** 运行时会话：turns 带派生态（流式标记、渲染 HTML），切页内不丢 */
interface Session {
  id: string;
  updatedAt: number;
  turns: ChatTurn[];
}

// 历史全存本地：新版按 { active, sessions } 整包存，旧单会话键只读一次做迁移
const STORAGE_KEY = 'blog_ai_sessions_v1';
const LEGACY_KEY = 'blog_ai_chat_v1';
const MAX_STORED_TURNS = 60;
const MAX_SESSIONS = 20;

const route = useRoute();

const sessions = ref<Session[]>([]);
const activeId = ref('');
const input = ref('');
const chatBusy = ref(false);
const chatBox = ref<HTMLElement | null>(null);
let aborter: AbortController | null = null;

const active = computed(() => sessions.value.find((s) => s.id === activeId.value));
// 模板与流式回调继续操作 turns，指向当前会话的回合数组
const turns = computed<ChatTurn[]>(() => active.value?.turns ?? []);

function scrollChat(): void {
  nextTick(() => {
    const el = chatBox.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
}

/** 把 [1] [2] 角标换成可点击的引用链接（内容已由 markdown-it 转义） */
function linkifyCitations(html: string): string {
  return html.replace(/\[(\d+)\]/g, (m, n) => {
    return `<a class="cite-ref" data-cite="${n}" href="javascript:void(0)">[${n}]</a>`;
  });
}

function onChatClick(e: MouseEvent): void {
  const el = (e.target as HTMLElement).closest?.('.cite-ref');
  if (!el) return;
  const turnEl = el.closest('[data-turn]');
  const turnIdx = Number(turnEl?.getAttribute('data-turn'));
  // 后端角标从 1 开始，按 citations[].index 精确匹配
  const cite = turns.value[turnIdx]?.citations?.find((c) => c.index === Number(el.getAttribute('data-cite')));
  if (cite) window.open(`/blog/post/${encodeURIComponent(cite.slug)}`, '_blank');
}

/** 相关度展示：RRF 融合分绝对值很小，按本次最高分归一化成百分比更直观 */
function pct(score: number, list: BlogCitation[]): string {
  const max = Math.max(...list.map((r) => r.score), 1e-9);
  return `${Math.max(1, Math.round((score / max) * 100))}%`;
}

/** 把当前所有会话写 localStorage：空会话只有当前激活的可留，超上限按活跃度修剪 */
function persist(): void {
  try {
    const list: PersistedSession[] = sessions.value
      .map((s) => {
        const slim: StoredTurn[] = s.turns
          .filter((t) => t.content.trim() || t.error || t.matches?.length)
          .slice(-MAX_STORED_TURNS)
          .map(({ role, content, citations, matches, error }) => ({
            role,
            content,
            ...(citations?.length ? { citations } : {}),
            ...(matches?.length ? { matches } : {}),
            ...(error ? { error } : {}),
          }));
        return { id: s.id, title: deriveTitle(slim), updatedAt: s.updatedAt, turns: slim };
      })
      .filter((s) => s.turns.length || s.id === activeId.value)
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, MAX_SESSIONS);
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ active: activeId.value, sessions: list }));
  } catch {
    /* 忽略：配额满/隐私模式写失败不影响对话进行 */
  }
}

function uid(): string {
  return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** 会话标题 = 首条非空提问（截 24 字）；新会话没有内容时列表显占位文案 */
function deriveTitle(turnList: { role: string; content: string }[]): string {
  const first = turnList.find((t) => t.role === 'user' && t.content.trim());
  return first ? first.content.trim().slice(0, 24) : '';
}

function sessionTitle(s: Session): string {
  return deriveTitle(s.turns) || '新会话';
}

function fmtTime(ts: number): string {
  const diff = Date.now() - ts;
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return new Date(ts).toLocaleDateString();
}

/** 落盘回合 → 运行时回合：助手正文按存下的 markdown 重新渲染 */
function hydrate(t: StoredTurn): ChatTurn {
  const turn: ChatTurn = { role: t.role, content: t.content };
  if (t.citations?.length) turn.citations = t.citations;
  if (t.matches?.length) turn.matches = t.matches;
  if (t.error) turn.error = t.error;
  if (t.role === 'assistant' && t.content) {
    turn.html = linkifyCitations(renderMarkdown(t.content));
  }
  return turn;
}

function newSession(force = false): void {
  // 当前已经是空会话就不再堆一个
  if (!force && active.value && !active.value.turns.length) return;
  const s: Session = { id: uid(), updatedAt: Date.now(), turns: [] };
  sessions.value.unshift(s);
  activeId.value = s.id;
  persist();
}

function switchSession(id: string): void {
  if (id === activeId.value) return;
  if (chatBusy.value) {
    message.info('回答生成中，等这条回复结束后再切换');
    return;
  }
  activeId.value = id;
  scrollChat();
  persist();
}

function removeSession(id: string): void {
  if (chatBusy.value) {
    message.info('回答生成中，稍后再删除');
    return;
  }
  const idx = sessions.value.findIndex((s) => s.id === id);
  if (idx < 0) return;
  const wasActive = sessions.value[idx].id === activeId.value;
  sessions.value.splice(idx, 1);
  if (wasActive) {
    if (!sessions.value.length) newSession(true);
    else activeId.value = sessions.value[0].id;
  }
  persist();
}

/** 读本地会话 + 迁移旧版单会话数据；保证总有一个激活会话 */
function loadSessions(): void {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(STORAGE_KEY);
  } catch {
    raw = null;
  }
  if (raw) {
    try {
      const file = JSON.parse(raw) as { active?: string; sessions?: PersistedSession[] };
      if (Array.isArray(file.sessions)) {
        sessions.value = file.sessions
          .filter((s) => Array.isArray(s.turns))
          .map((s) => ({
            id: s.id || uid(),
            updatedAt: s.updatedAt || 0,
            turns: s.turns.map(hydrate),
          }));
        activeId.value =
          file.active && sessions.value.some((s) => s.id === file.active)
            ? file.active
            : (sessions.value[0]?.id ?? '');
      }
    } catch {
      try {
        localStorage.removeItem(STORAGE_KEY);
      } catch {
        /* 忽略 */
      }
    }
  }
  // 旧版单会话（纯数组）迁移：整包变成一个会话插到最前，然后清掉旧键
  try {
    const legacy = localStorage.getItem(LEGACY_KEY);
    if (legacy) {
      const list = JSON.parse(legacy) as StoredTurn[];
      if (Array.isArray(list) && list.length) {
        const s: Session = {
          id: uid(),
          updatedAt: Date.now(),
          turns: list.map(hydrate),
        };
        sessions.value.unshift(s);
        activeId.value = s.id;
      }
      localStorage.removeItem(LEGACY_KEY);
    }
  } catch {
    /* 忽略：坏数据不阻碍进页 */
  }
  if (!sessions.value.length) newSession(true);
  else if (!activeId.value) activeId.value = sessions.value[0].id;
  if (turns.value.length) scrollChat();
}

async function sendChat(preset?: string): Promise<void> {
  const text = (preset ?? input.value).trim();
  if (!text || chatBusy.value) return;
  input.value = '';
  // 先取历史再入列，避免把本轮提问重复带进请求
  const history: ChatMessage[] = turns.value
    .filter((t) => t.content.trim()) // 空内容的助手回合（如上次报错）不回喂，后端会 422
    .map(({ role, content }) => ({ role, content }));
  const userTurn: ChatTurn = { role: 'user', content: text, matches: [], matching: true };
  turns.value.push(userTurn);
  const assistant: ChatTurn = { role: 'assistant', content: '', html: '', streaming: true, citations: [] };
  turns.value.push(assistant);
  scrollChat();

  // 语义检索与回答并行：命中挂在问题气泡下方，失败静默（不打断对话）
  blogApi
    .search(text, 5)
    .then((res) => {
      userTurn.matches = res.items;
    })
    .catch(() => {
      userTurn.matches = [];
    })
    .finally(() => {
      userTurn.matching = false;
      scrollChat();
      persist();
    });

  chatBusy.value = true;
  if (active.value) active.value.updatedAt = Date.now();
  aborter = new AbortController();
  const rawText = () => assistant.content;
  const rerender = () => {
    assistant.html = linkifyCitations(renderMarkdown(rawText()));
  };

  try {
    await chatStream(history.concat([{ role: 'user', content: text }]), (chunk: ChatChunk) => {
      if (chunk.citations?.length) {
        assistant.citations = chunk.citations;
      }
      if (chunk.content) {
        assistant.content += chunk.content;
        rerender();
        scrollChat();
      }
      if (chunk.error) {
        assistant.error = chunk.error;
      }
    }, aborter.signal);
  } catch (e) {
    if (!assistant.content) assistant.error = (e as Error).message || '对话失败';
  } finally {
    assistant.streaming = false;
    chatBusy.value = false;
    aborter = null;
    scrollChat();
    persist();
  }
}

function resetChat(): void {
  aborter?.abort();
  // 只清当前会话；其他会话历史原样留在列表
  if (active.value) active.value.turns = [];
  persist();
}

onBeforeUnmount(() => aborter?.abort());

// 先恢复本地会话列表，再处理 ?q= 自动提问（带词进来时是接在当前会话后续问）
loadSessions();

// ?q= 进入（比如从别处带关键词跳来）自动提出这个问题
const initialQuery = typeof route.query.q === 'string' ? route.query.q : '';
if (initialQuery.trim()) sendChat(initialQuery);
</script>

<template>
  <div class="blog-card chat-card">
    <div class="chat-layout">
      <!-- 会话侧栏：新建/切换/删除，全部只存本地；窄屏收成顶部横向 chips -->
      <aside class="session-side">
        <a-button size="small" block class="new-btn" @click="newSession()">
          <template #icon><PlusOutlined /></template>
          新建会话
        </a-button>
        <div class="session-list">
          <div
            v-for="s in sessions"
            :key="s.id"
            class="session-item"
            :class="{ active: s.id === activeId }"
            @click="switchSession(s.id)"
          >
            <div class="session-main">
              <span class="session-name">{{ sessionTitle(s) }}</span>
              <span class="session-time">{{ fmtTime(s.updatedAt) }}</span>
            </div>
            <span
              class="session-del"
              title="删除会话"
              @click.stop="removeSession(s.id)"
            ><DeleteOutlined /></span>
          </div>
        </div>
      </aside>

      <div class="chat-main">
        <div class="chat-head">
          <h2 class="chat-title"><RobotOutlined class="title-icon" /><span class="title-text">AI 对话搜索</span></h2>
          <span class="chat-sub">基于博客内容回答 · 自动附上命中文章与引用 · 多会话历史保存在本地</span>
          <a-button v-if="turns.length" type="text" size="small" class="clear-btn" @click="resetChat">
            <template #icon><ClearOutlined /></template>
            清空本会话
          </a-button>
        </div>

        <div ref="chatBox" class="chat-box" @click="onChatClick">
          <div v-if="!turns.length" class="chat-empty">
            <div class="empty-icon"><SearchOutlined /></div>
            <p class="empty-title">想知道这篇博客里写过什么？直接问</p>
            <p class="empty-hint">回答会边检索边流式生成，命中的文章展示在问题下方</p>
          </div>
          <div v-for="(turn, i) in turns" :key="i" class="turn" :class="turn.role">
            <a-avatar :size="28" class="turn-avatar" :class="turn.role">
              <template #icon>
                <UserOutlined v-if="turn.role === 'user'" />
                <RobotOutlined v-else />
              </template>
            </a-avatar>
            <div class="turn-body">
              <div class="turn-bubble">
                <template v-if="turn.role === 'user'">{{ turn.content }}</template>
                <template v-else>
                  <!-- 引用角标经 linkifyCitations 处理，点击新窗口打开对应文章 -->
                  <div v-if="turn.html" class="markdown-body chat-md" :data-turn="i" v-html="turn.html" />
                  <span v-if="turn.streaming" class="streaming-dot">▍</span>
                  <div v-if="turn.error" class="turn-error">{{ turn.error }}</div>
                  <div v-if="turn.citations?.length" class="turn-cites">
                    引用：
                    <router-link
                      v-for="c in turn.citations"
                      :key="c.index"
                      :to="{ name: 'post', params: { slug: c.slug } }"
                      class="cite-link"
                    >[{{ c.index }}] {{ c.title }}</router-link>
                  </div>
                </template>
              </div>

              <!-- 提问的语义检索命中卡片带 -->
              <div v-if="turn.role === 'user' && (turn.matching || turn.matches?.length)" class="match-list">
                <div v-if="turn.matching" class="match-loading">
                  <a-spin size="small" /> <span>正在检索本站文章…</span>
                </div>
                <router-link
                  v-for="m in turn.matches"
                  :key="m.index"
                  :to="{ name: 'post', params: { slug: m.slug } }"
                  class="match-card"
                >
                  <div class="match-top">
                    <span class="match-index">{{ m.index }}</span>
                    <span class="match-title">{{ m.title }}</span>
                    <span class="match-score">{{ pct(m.score, turn.matches ?? []) }}</span>
                  </div>
                  <p class="match-snippet">{{ m.snippet }}</p>
                </router-link>
              </div>
            </div>
          </div>
        </div>

        <div class="chat-input">
          <a-textarea
            id="chat-input"
            v-model:value="input"
            :rows="2"
            :maxlength="2000"
            placeholder="问我任何博客里写过的问题，Enter 发送、Shift+Enter 换行"
            @press-enter.prevent="sendChat()"
          />
          <a-button
            type="primary"
            block
            :loading="chatBusy"
            :disabled="!input.trim()"
            @click="sendChat()"
          >
            <template #icon><SendOutlined /></template>
            发送
          </a-button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* --- 双栏布局：左会话列表 + 右对话主体 --- */
.chat-layout {
  display: flex;
  gap: 16px;
  align-items: stretch;
  min-width: 0;
}

.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.session-side {
  width: 212px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  border-right: 1px solid var(--border);
  padding-right: 14px;
}

.new-btn {
  flex-shrink: 0;
  border-style: dashed;
}

.session-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow-y: auto;
  max-height: calc(100vh - 400px);
  min-height: 120px;
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
  padding-right: 2px;
}

.session-item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 7px 10px;
  border-radius: 9px;
  border: 1px solid transparent;
  cursor: pointer;
  min-width: 0;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.session-item:hover {
  background: var(--bg-soft);
}

.session-item.active {
  background: var(--primary-soft);
  border-color: color-mix(in srgb, var(--primary) 22%, var(--border));
}

.session-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  line-height: 1.4;
}

.session-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-item.active .session-name {
  color: var(--primary);
}

.session-time {
  font-size: 11px;
  color: var(--text-soft);
}

/* 删除按钮 hover 才现身，列表平时保持干净 */
.session-del {
  flex-shrink: 0;
  padding: 2px 4px;
  border-radius: 6px;
  color: var(--text-soft);
  opacity: 0;
  transition: opacity 0.15s ease, color 0.15s ease;
}

.session-item:hover .session-del {
  opacity: 1;
}

.session-del:hover {
  color: #ff4d4f;
}

/* 对话容器是全屏阅读主体：压掉 .blog-card 默认的 hover 上浮（整卡抖动比列表卡片碍事），渐变线常亮 */
.chat-card:hover {
  transform: none;
}

.chat-card::before {
  opacity: 0.85;
}

.chat-card:hover::before {
  opacity: 1;
}

.chat-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.chat-title {
  margin: 0;
  font-size: 17px;
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 渐变文字只给标题文本，图标单独吃主色（background-clip 会让子元素 glyph 一起透明） */
.title-text {
  background: var(--grad);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.title-icon {
  color: var(--primary);
}

.chat-sub {
  flex: 1;
  min-width: 200px;
  font-weight: 400;
  font-size: 13px;
  color: var(--text-soft);
}

.clear-btn {
  flex-shrink: 0;
}

.chat-box {
  max-height: calc(100vh - 320px);
  min-height: 260px;
  overflow-y: auto;
  padding: 4px 6px 4px 4px;
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
}

.chat-box::-webkit-scrollbar {
  width: 8px;
}

.chat-box::-webkit-scrollbar-thumb {
  background: var(--border);
  border-radius: 4px;
}

/* --- 空态：居中大图标引导 --- */
.chat-empty {
  text-align: center;
  padding: 56px 16px;
  color: var(--text-soft);
}

.empty-icon {
  width: 64px;
  height: 64px;
  margin: 0 auto 18px;
  border-radius: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 26px;
  /* 彩底上的白图标属刻意配色，不跟着主题变 */
  color: #fff;
  background: var(--grad);
  box-shadow: var(--shadow-lift);
}

.empty-title {
  margin: 0 0 6px;
  font-size: 17px;
  font-weight: 600;
  color: var(--text);
}

.empty-hint {
  margin: 0;
  font-size: 13px;
}

/* --- 回合 --- */
.turn {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
  animation: turn-in 0.18s ease;
}

@keyframes turn-in {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}

.turn.user {
  flex-direction: row-reverse;
}

.turn-avatar {
  flex-shrink: 0;
  border: none;
}

/* 覆盖 antd 默认的深灰头像底，两侧用不同身份色 */
.turn.user .turn-avatar {
  background: var(--primary-soft);
  color: var(--primary);
}

.turn.assistant .turn-avatar {
  background: var(--grad);
  color: #fff; /* 彩底白 glyph：刻意配色，不受主题管 */
}

/* 气泡 + 命中卡片带的纵向容器；用户侧整体右靠 */
.turn-body {
  max-width: 78%;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.turn.user .turn-body {
  align-items: flex-end;
}

.turn-bubble {
  background: var(--bg-soft);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 8px 14px;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 靠近头像一侧收小圆角，做出「对话尾角」的指向感 */
.turn.assistant .turn-bubble {
  white-space: normal;
  border-top-left-radius: 4px;
}

.turn.user .turn-bubble {
  background: var(--primary-soft);
  border-color: color-mix(in srgb, var(--primary) 22%, var(--border));
  color: var(--text);
  border-top-right-radius: 4px;
}

.chat-md :deep(p:first-child) {
  margin-top: 0;
}

.chat-md :deep(p:last-child) {
  margin-bottom: 0;
}

.streaming-dot {
  animation: blink 1s steps(2) infinite;
  color: var(--primary);
}

@keyframes blink {
  50% { opacity: 0; }
}

.turn-error {
  color: #ff4d4f;
  font-size: 13px;
  margin-top: 6px;
  padding: 6px 10px;
  border-radius: 8px;
  border: 1px solid color-mix(in srgb, #ff4d4f 35%, var(--border));
  background: color-mix(in srgb, #ff4d4f 8%, var(--bg-soft));
}

.turn-cites {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--border);
  font-size: 12px;
  color: var(--text-soft);
}

/* 引用做成小胶囊，比普通文字链接更像可点目标 */
.cite-link {
  display: inline-flex;
  align-items: center;
  padding: 1px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-soft);
  color: var(--text-soft);
  font-size: 12px;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.cite-link:hover {
  color: var(--primary);
  border-color: var(--primary);
  text-decoration: none;
}

/* --- 命中文章卡片带（问题气泡下方） --- */
.match-list {
  width: 100%;
  max-width: 460px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.turn.user .match-list {
  margin-left: auto;
}

.match-loading {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-soft);
  padding: 8px 12px;
  border: 1px dashed var(--border);
  border-radius: 10px;
  background: var(--bg-soft);
}

.match-card {
  position: relative;
  overflow: hidden;
  display: block;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 12px;
  color: var(--text);
  transition: border-color 0.15s ease, transform 0.15s ease, box-shadow 0.15s ease;
}

/* hover 时左侧亮一条渐变准线，和卡片顶部渐变线同一套语言 */
.match-card::before {
  content: '';
  position: absolute;
  left: 0;
  top: 10px;
  bottom: 10px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--grad);
  opacity: 0;
  transition: opacity 0.15s ease;
}

.match-card:hover {
  text-decoration: none;
  color: var(--text);
  border-color: var(--primary);
  transform: translateY(-1px);
  box-shadow: var(--shadow);
}

.match-card:hover::before {
  opacity: 1;
}

.match-top {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.match-index {
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--primary);
  background: var(--primary-soft);
  min-width: 20px;
  height: 20px;
  padding: 0 6px;
  border-radius: 6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.match-title {
  flex: 1;
  font-weight: 600;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.match-score {
  flex-shrink: 0;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--accent);
  padding: 1px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-soft);
}

.match-snippet {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-soft);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* --- 唯一输入区 --- */
.chat-input {
  margin-top: 12px;
  display: flex;
  gap: 10px;
  align-items: stretch;
}

.chat-input .ant-btn {
  width: 110px;
  flex-shrink: 0;
}

.chat-input :deep(.ant-input) {
  border-radius: 10px;
}

/* 发送键吃全站渐变；禁用态要换回中性色，不然渐变底看着像还能点 */
.chat-input :deep(.ant-btn-primary:not([disabled])) {
  background: var(--grad);
  border: none;
  border-radius: 10px;
  font-weight: 600;
}

.chat-input :deep(.ant-btn-primary[disabled]) {
  background: var(--bg-soft);
  color: var(--text-soft);
  border: 1px solid var(--border);
}

@media (max-width: 720px) {
  /* 窄屏：会话列表收成顶部横向滑动的 chips，不吃掉正文高度 */
  .chat-layout {
    flex-direction: column;
    gap: 12px;
  }

  .session-side {
    width: auto;
    flex-direction: row;
    align-items: stretch;
    gap: 8px;
    border-right: none;
    padding-right: 0;
    border-bottom: 1px solid var(--border);
    padding-bottom: 12px;
  }

  .new-btn {
    width: auto;
    flex-shrink: 0;
  }

  .session-list {
    flex: 1;
    min-width: 0;
    flex-direction: row;
    overflow-x: auto;
    overflow-y: hidden;
    max-height: none;
    min-height: 0;
  }

  .session-item {
    min-width: 150px;
    flex-shrink: 0;
  }

  .turn-body {
    max-width: 88%;
  }

  .chat-input .ant-btn {
    width: 76px;
  }
}
</style>
