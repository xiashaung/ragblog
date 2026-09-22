<script setup lang="ts">
// AI 对话搜索页：全站只留一个输入框——提问即同时做「语义检索 + AI 流式回答」，
// 命中文章以卡片带展示在问题气泡下方，答案附引用角标；多轮追问直接继续输入。
// 进入时 URL 带 ?q= 会自动提出这个问题。
import { nextTick, onBeforeUnmount, ref } from 'vue';
import { useRoute } from 'vue-router';
import {
  ClearOutlined,
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

const route = useRoute();

const turns = ref<ChatTurn[]>([]);
const input = ref('');
const chatBusy = ref(false);
const chatBox = ref<HTMLElement | null>(null);
let aborter: AbortController | null = null;

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
    });

  chatBusy.value = true;
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
  }
}

function resetChat(): void {
  aborter?.abort();
  turns.value = [];
}

onBeforeUnmount(() => aborter?.abort());

// ?q= 进入（比如从别处带关键词跳来）自动提出这个问题
const initialQuery = typeof route.query.q === 'string' ? route.query.q : '';
if (initialQuery.trim()) sendChat(initialQuery);
</script>

<template>
  <div class="blog-card chat-card">
    <div class="chat-head">
      <h2 class="chat-title"><RobotOutlined /> AI 对话搜索</h2>
      <span class="chat-sub">基于博客内容回答 · 自动附上命中文章与引用来源</span>
      <a-button v-if="turns.length" type="text" size="small" class="clear-btn" @click="resetChat">
        <template #icon><ClearOutlined /></template>
        清空
      </a-button>
    </div>

    <div ref="chatBox" class="chat-box" @click="onChatClick">
      <div v-if="!turns.length" class="chat-empty">
        <div class="empty-icon"><SearchOutlined /></div>
        <p class="empty-title">想知道这篇博客里写过什么？直接问</p>
        <p class="empty-hint">回答会边检索边流式生成，命中的文章展示在问题下方</p>
      </div>
      <div v-for="(turn, i) in turns" :key="i" class="turn" :class="turn.role">
        <a-avatar :size="28" class="turn-avatar">
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
                <span class="match-index">[{{ m.index }}]</span>
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
</template>

<style scoped>
.chat-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 14px;
}

.chat-title {
  margin: 0;
  font-size: 17px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.chat-sub {
  flex: 1;
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
  padding: 4px;
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
  color: var(--primary);
  background: var(--primary-soft);
  border: 1px solid var(--border);
  box-shadow: var(--shadow);
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
}

.turn.user {
  flex-direction: row-reverse;
}

.turn-avatar {
  flex-shrink: 0;
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
  border-radius: 10px;
  padding: 8px 14px;
  white-space: pre-wrap;
  word-break: break-word;
}

.turn.user .turn-bubble {
  background: var(--primary-soft);
  color: var(--text);
}

.turn.assistant .turn-bubble {
  white-space: normal;
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
}

.turn-cites {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--border);
  font-size: 12px;
  color: var(--text-soft);
}

.cite-link {
  margin-right: 10px;
  font-size: 12px;
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
  padding: 2px 4px;
}

.match-card {
  display: block;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 12px;
  color: var(--text);
  transition: border-color 0.15s ease, transform 0.15s ease;
}

.match-card:hover {
  text-decoration: none;
  color: var(--text);
  border-color: var(--primary);
  transform: translateY(-1px);
}

.match-top {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.match-index {
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 12px;
  color: var(--primary);
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
  font-size: 12px;
  color: var(--accent);
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

@media (max-width: 720px) {
  .turn-body {
    max-width: 88%;
  }

  .chat-input .ant-btn {
    width: 76px;
  }
}
</style>
