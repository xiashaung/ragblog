<script setup lang="ts">
/**
 * AI 对话 —— 流式多轮对话 + 后端持久化的多会话。
 *   布局：左侧会话列表 + 生成参数；右侧对话流 + 底部输入（与智能问数页同一套结构）。
 *   模型 / provider 全由后端 .env 决定，这里只读展示，不给前端选择器。
 *   持久化：一轮生成**完成后**整轮入库（失败的半截回答不存），刷新或换设备都能回放。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { message } from 'ant-design-vue';
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  PaperClipOutlined,
  PlusOutlined,
  ReloadOutlined,
  StopOutlined,
} from '@ant-design/icons-vue';
import { chatApi, streamChat } from '@/api';
import { useBusinessStore } from '@/store/modules/business';
import RichText from '@/components/RichText.vue';
import { fileToDataUrl, isImageFile } from '@/utils/rich';
import type {
  ChatMessage,
  ChatSession,
  ChatToolEvent,
  ChatUsage,
  WebMode,
  WebSource,
} from '@/typings';

/** 一次联网步骤（call 与 result 合并成一条展示） */
interface WebStep {
  key: string;
  name: string;
  round: number;
  detail: string;
  done: boolean;
  ok: boolean;
  error: string | null;
  sources: WebSource[];
}

/** 对话流里的一条消息 */
interface UiMsg {
  key: string;
  role: 'user' | 'assistant';
  content: string;
  /** 思考过程（深度思考模型的推理内容） */
  thinking?: string;
  /** 随消息附带的图片 data URL */
  images: string[];
  /** 因体积过大未入库的图片张数（回放时明确提示，而不是默默缺图） */
  imagesDropped: number;
  usage: ChatUsage | null;
  createdAt: number;
  /** 正在流式生成 */
  pending?: boolean;
  /** 用户主动停止（这一版不入库） */
  stopped?: boolean;
  /** 这一版失败原因（失败的回答不入库） */
  error?: string;
  /** 本轮联网检索步骤（只活在本地：历史不存中间过程） */
  webSteps?: WebStep[];
  /** 本轮命中的链接（随回答入库，回放时还能看到来源） */
  sources?: WebSource[];
}

const business = useBusinessStore();
const backend = computed<any>(() => business.activeProvider);
const backendDesc = computed(() =>
  backend.value
    ? `${backend.value.name} · ${backend.value.chat_model || '默认模型'}`
    : '后端配置加载中…',
);

/* ---------------- 生成参数（provider / model 不在这里，由后端配置） ---------------- */
const form = reactive({
  temperature: 0.7,
  maxTokens: 2048,
  jsonMode: false,
  /** 联网：auto=模型自己判断要不要查（默认），forced=首轮强制先查 */
  webMode: 'auto' as WebMode,
});
const paramsOpen = ref<string[]>([]);

// 三态的区别写清楚，否则用户分不清「自动」和「必查」到底会不会去搜
const WEB_MODE_HINTS: Record<WebMode, string> = {
  auto: '模型自己判断：只在时效性内容或不确定的事实时才搜索',
  forced: '每次先搜一轮再作答（问“今天/最新”类问题时用）',
  off: '不挂工具，只凭模型记忆回答',
};
const webModeHint = computed(() => WEB_MODE_HINTS[form.webMode]);

/* ---------------- 会话 ---------------- */
const sessions = ref<ChatSession[]>([]);
const currentId = ref('');
const current = computed(() => sessions.value.find((s) => s.id === currentId.value) ?? null);
const currentTitle = computed(() => current.value?.title || '新对话');

/* ---------------- 对话流 ---------------- */
const messages = ref<UiMsg[]>([]);
const input = ref('');
/** 一次附图上限（后端 ChatMessageIn.images 也有 max_length 兜底） */
const MAX_ATTACH = 4;
const pendingImages = ref<string[]>([]);
const streaming = ref(false);
const listRef = ref<HTMLElement | null>(null);
let controller: AbortController | null = null;
let keySeq = 0;
const mkKey = (): string => `m-${Date.now().toString(36)}-${(keySeq++).toString(36)}`;

// 流式期间每个 chunk 都可能改变高度，用计数器合并同一 tick 的多次滚动请求
const scrollTick = ref(0);
function scrollToBottom(): void {
  nextTick(() => {
    const el = listRef.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
}
watch(scrollTick, scrollToBottom);
watch(() => messages.value.length, scrollToBottom);

const usageTotal = computed(() =>
  messages.value.reduce((sum, m) => sum + (m.usage?.total_tokens ?? 0), 0),
);

const samples = [
  '用三句话解释什么是向量数据库',
  '帮我把一段需求改写成技术要点，该按什么结构组织？',
  '写一条只统计本月订单数量的 SQL，并说明怎么防注入',
  '把“只读事务”这个词解释给非技术同学听',
];

function fmtTime(ms: number): string {
  return new Date(ms).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

/** 会话列表里的相对时间：今天只显示时分，更早的带上日期 */
function fmtAgo(sec: number): string {
  const d = new Date(sec * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay
    ? d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
}

/* ---------------- 会话读写 ---------------- */
async function loadSessions(): Promise<void> {
  try {
    const data = await chatApi.sessions();
    sessions.value = data.sessions ?? [];
  } catch (e: any) {
    message.error(e?.detail || '读取会话列表失败');
  }
}

function toUiMsg(m: ChatMessage): UiMsg {
  return {
    key: m.id,
    role: m.role,
    content: m.content,
    thinking: m.thinking ?? undefined,
    images: m.images ?? [],
    imagesDropped: m.images_dropped ?? 0,
    usage: m.usage ?? null,
    sources: m.web_sources ?? [],
    createdAt: m.created_at * 1000,
  };
}

async function openSession(id: string): Promise<void> {
  if (streaming.value) {
    message.warning('正在生成回答，请先停止再切换会话');
    return;
  }
  currentId.value = id;
  try {
    const data = await chatApi.messages(id);
    messages.value = (data.messages ?? []).map(toUiMsg);
  } catch (e: any) {
    message.error(e?.detail || '读取会话内容失败');
  }
}

/** 就地更新（或插入）后端返回的会话快照：自动改名、消息条数都靠它同步 */
function upsertSession(snapshot: ChatSession): void {
  const idx = sessions.value.findIndex((s) => s.id === snapshot.id);
  if (idx >= 0) {
    // 活跃时间变了要把顺序顶回最前，保持与后端 updated_at 排序一致
    sessions.value.splice(idx, 1);
  }
  sessions.value.unshift(snapshot);
}

async function newSession(): Promise<ChatSession | null> {
  try {
    const created = await chatApi.create();
    sessions.value.unshift(created);
    currentId.value = created.id;
    messages.value = [];
    return created;
  } catch (e: any) {
    message.error(e?.detail || '新建会话失败');
    return null;
  }
}

async function onNewSession(): Promise<void> {
  if (streaming.value) {
    message.warning('正在生成回答，请先停止再新建会话');
    return;
  }
  await newSession();
}

async function onRemoveSession(s: ChatSession): Promise<void> {
  try {
    await chatApi.remove(s.id);
    sessions.value = sessions.value.filter((x) => x.id !== s.id);
    if (currentId.value === s.id) {
      currentId.value = '';
      messages.value = [];
    }
    message.success('会话已删除');
  } catch (e: any) {
    message.error(e?.detail || '删除会话失败');
  }
}

/* 改名弹窗 */
const renameOpen = ref(false);
const renameTitle = ref('');
const renameId = ref('');
function startRename(s: ChatSession): void {
  renameId.value = s.id;
  renameTitle.value = s.title;
  renameOpen.value = true;
}
async function doRename(): Promise<void> {
  const title = renameTitle.value.trim();
  if (!title) {
    message.warning('标题不能为空');
    return;
  }
  try {
    const updated = await chatApi.rename(renameId.value, title);
    const idx = sessions.value.findIndex((s) => s.id === renameId.value);
    if (idx >= 0) sessions.value.splice(idx, 1, updated);
    renameOpen.value = false;
  } catch (e: any) {
    message.error(e?.detail || '改名失败');
  }
}

/* ---------------- 图片附件 ---------------- */
async function onPickImages(ev: Event): Promise<void> {
  const el = ev.target as HTMLInputElement;
  const picked = Array.from(el.files || []).filter(isImageFile);
  el.value = '';
  for (const file of picked) {
    if (pendingImages.value.length >= MAX_ATTACH) {
      message.warning(`单次最多附加 ${MAX_ATTACH} 张图片`);
      break;
    }
    try {
      pendingImages.value.push(await fileToDataUrl(file));
    } catch {
      message.error(`读取图片失败：${file.name}`);
    }
  }
}

function removeImage(index: number): void {
  pendingImages.value.splice(index, 1);
}

/** 对话历史 → 后端 OpenAI 兼容 messages：带图的用户消息用 content parts */
function buildMessages(items: UiMsg[]): unknown[] {
  return items
    .filter((m) => !m.error) // 失败的那一版不喂回模型
    .map((m) => {
      if (m.role === 'user' && m.images.length) {
        return {
          role: m.role,
          content: [
            { type: 'text', text: m.content },
            ...m.images.map((url) => ({ type: 'image_url', image_url: { url } })),
          ],
        };
      }
      return { role: m.role, content: m.content };
    });
}

/* ---------------- 发送 / 停止 / 再答一次 ---------------- */
/**
 * 跑一轮生成。
 * @param sessionId 落库目标会话
 * @param persistUser 是否连同这条提问一起入库（再答一次时提问已在库里）
 * @param history 喂给模型的上下文（不含正在生成的这条）
 * @param question/images 本轮提问内容，用于入库
 */
async function runTurn(
  sessionId: string,
  question: string,
  images: string[],
  history: UiMsg[],
  persistUser: boolean,
): Promise<void> {
  const reply: UiMsg = {
    key: mkKey(),
    role: 'assistant',
    content: '',
    images: [],
    imagesDropped: 0,
    usage: null,
    sources: [],
    webSteps: [],
    createdAt: Date.now(),
    pending: true,
  };
  messages.value.push(reply);
  // 注意：要用数组里的响应式代理改内容，直接改 push 进去的原始对象不会触发重渲染
  const live = messages.value[messages.value.length - 1];

  controller = new AbortController();
  streaming.value = true;
  let failure = '';
  let aborted = false;
  try {
    await streamChat(
      {
        messages: buildMessages(history),
        temperature: form.temperature,
        max_tokens: Math.max(1, form.maxTokens | 0),
        response_format: form.jsonMode ? { type: 'json_object' } : null,
        web_mode: form.webMode,
      },
      (chunk) => {
        if (chunk.content) live.content += chunk.content;
        if (chunk.reasoning) live.thinking = (live.thinking || '') + chunk.reasoning;
        if (chunk.usage) live.usage = chunk.usage;
        if (chunk.tool) applyToolEvent(live, chunk.tool);
        if (chunk.error) failure = chunk.error;
        scrollTick.value += 1;
      },
      controller.signal,
    );
  } catch (e: any) {
    if (e?.name === 'AbortError') {
      aborted = true;
    } else {
      failure = e?.message || '对话请求失败';
    }
  } finally {
    live.pending = false;
    streaming.value = false;
    controller = null;
  }

  if (failure) {
    live.error = failure;
    message.error(failure);
    return; // 失败的一版不入历史
  }
  if (!live.content && !aborted) {
    // 没报错却一个字没返（上游拒绝或只回空格）：当成失败处理，不存空消息
    live.error = '模型没有返回内容（检查后端是否可达 / 是否需换模型）';
    message.warning(live.error);
    return;
  }
  if (aborted) {
    live.stopped = true;
    if (!live.content) {
      // 一个字都没生成就撤掉这条空气泡
      messages.value = messages.value.filter((m) => m.key !== live.key);
      return;
    }
  }
  const items: ChatAppendItem[] = [];
  if (persistUser) items.push({ role: 'user', content: question, images });
  items.push({
    role: 'assistant',
    content: live.content,
    thinking: live.thinking || undefined,
    usage: live.usage ?? undefined,
    // 只存链接不存搜索全文：中间产物回喂过模型就完事，下次再搜结果也不一样
    web_sources: live.sources?.length ? live.sources : undefined,
  });
  await persist(sessionId, items, live);
}

/**
 * 把后端透出的工具事件并到这条回答上。
 *
 * call 与 result 是两条独立事件（中间隔着一次真实网络请求），按 `name-round`
 * 合并成同一步，界面上就是一条「搜索：xxx」从转圈变成完成，而不是列出两行。
 */
function applyToolEvent(msg: UiMsg, ev: ChatToolEvent): void {
  if (!msg.webSteps) msg.webSteps = [];
  const steps = msg.webSteps;
  const key = `${ev.name}-${ev.round}`;
  const sources = ev.sources ?? [];
  if (ev.phase === 'call') {
    steps.push({
      key,
      name: ev.name,
      round: ev.round,
      detail: ev.detail || ev.name,
      done: false,
      ok: true,
      error: null,
      sources: [],
    });
    return;
  }
  const step = steps.find((s) => s.key === key);
  if (step) {
    step.done = true;
    step.ok = ev.ok !== false;
    step.error = ev.error ?? null;
    step.sources = sources;
  } else {
    // 只收到 result（call 事件被中间层吞掉之类）也要显示，别静默少一步
    steps.push({
      key,
      name: ev.name,
      round: ev.round,
      detail: ev.detail || ev.name,
      done: true,
      ok: ev.ok !== false,
      error: ev.error ?? null,
      sources,
    });
  }
  // 引用链接按 URL 去重并入：多轮搜索常命中同一条，重复列出来没法看
  if (!msg.sources) msg.sources = [];
  const known = new Set(msg.sources.map((s) => s.url));
  for (const s of sources) {
    if (known.has(s.url)) continue;
    known.add(s.url);
    msg.sources.push(s);
  }
}

interface ChatAppendItem {
  role: 'user' | 'assistant';
  content: string;
  thinking?: string;
  images?: string[];
  usage?: ChatUsage;
  web_sources?: WebSource[];
}

/** 从后往前找最后一条提问的下标（findLastIndex 需 ES2023 库，不一定在编译目标里） */
function lastUserIndex(): number {
  for (let i = messages.value.length - 1; i >= 0; i -= 1) {
    if (messages.value[i].role === 'user') return i;
  }
  return -1;
}

/** 入库；失败只提示不抛错——对话内容已在本地，不该因为存不上就报“生成失败” */
async function persist(sessionId: string, items: ChatAppendItem[], live: UiMsg): Promise<void> {
  try {
    const res = await chatApi.append(sessionId, { messages: items });
    upsertSession(res.session);
    // 用后端返回的 id 换掉本地 key，回放时同一条不会被当成两条
    const stored = res.messages[res.messages.length - 1];
    const idx = messages.value.findIndex((m) => m.key === live.key);
    if (stored && idx >= 0) {
      messages.value[idx] = { ...messages.value[idx], key: stored.id };
    }
    // 图片过大被后端丢弃时，把张数带回这条提问气泡（否则用户以为图还在）
    const dropped = res.messages.find((m) => m.role === 'user' && m.images_dropped > 0);
    if (dropped) {
      const qIdx = lastUserIndex();
      if (qIdx >= 0) messages.value[qIdx].imagesDropped = dropped.images_dropped;
    }
  } catch (e: any) {
    message.warning(e?.detail || '回答未能存入历史，刷新后这一轮会丢失');
  }
}

async function onSend(): Promise<void> {
  if (streaming.value) return;
  const question = input.value.trim();
  const images = [...pendingImages.value];
  if (!question && !images.length) {
    message.warning('请输入问题或附加图片');
    return;
  }

  let session = current.value;
  if (!session) {
    session = await newSession(); // 没选会话就直接开问：自动建一个
    if (!session) return;
  }
  messages.value.push({
    key: mkKey(),
    role: 'user',
    content: question,
    images,
    imagesDropped: 0,
    usage: null,
    createdAt: Date.now(),
  });
  input.value = '';
  pendingImages.value = [];
  // 快照取整条数组（含刚推进去的这条提问）：写成 slice(0, -1) 会把提问自己切掉，
  // 首轮上下文为空 → 后端 messages 的 min_length=1 报 422，后续轮则静默漏掉当前提问
  await runTurn(session.id, question, images, [...messages.value], true);
}

/** 再答一次：保留上一版回答，用同一个提问再生成一版追加在下面（历史只追加不覆盖） */
async function onRegenerate(): Promise<void> {
  if (streaming.value) return;
  const session = current.value;
  if (!session) {
    message.warning('还没有会话，先发一个问题吧');
    return;
  }
  const idx = lastUserIndex();
  if (idx < 0) {
    message.warning('还没有提问可以重答');
    return;
  }
  const target = messages.value[idx];
  // 只喂到这条提问为止（要把那一版回答换掉，而不是把旧答案也当上下文喂进去）
  await runTurn(
    session.id,
    target.content,
    target.images,
    messages.value.slice(0, idx + 1),
    false,
  );
}

function onStop(): void {
  controller?.abort();
}

function onPressEnter(ev: KeyboardEvent): void {
  if (ev.shiftKey) return; // Shift+Enter 换行
  ev.preventDefault();
  void onSend();
}

/* ---------------- 复制 ---------------- */
async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    message.success('已复制到剪贴板');
  } catch {
    message.error('浏览器拒绝了剪贴板写入，请手动选择文本');
  }
}

/** 思考过程展开状态：用 key 映射 */
const thinkingExpanded = ref<Record<string, boolean>>({});

function toggleThinking(m: UiMsg): void {
  thinkingExpanded.value[m.key] = !isThinkingExpanded(m);
}

function isThinkingExpanded(m: UiMsg): boolean {
  return !!thinkingExpanded.value[m.key];
}

function useSample(text: string): void {
  input.value = text;
}

onMounted(async () => {
  business.loadProviders();
  business.pollHealth();
  await loadSessions();
  // 回到上次聊的会话，而不是让人对着空白页重新找
  if (!currentId.value && sessions.value.length) {
    await openSession(sessions.value[0].id);
  }
});

onUnmounted(() => {
  controller?.abort(); // 离页即断流，别让后台继续吐
});
</script>

<template>
  <div class="chat-page">
    <!-- ---------- 左侧：会话列表 + 参数 ---------- -->
    <aside class="side">
      <a-button type="primary" block @click="onNewSession">
        <PlusOutlined /> 新对话
      </a-button>

      <div class="side-block side-sessions">
        <div class="side-title">会话（{{ sessions.length }}）</div>
        <div class="session-list">
          <div
            v-for="s in sessions"
            :key="s.id"
            class="session-item"
            :class="{ active: s.id === currentId }"
            @click="openSession(s.id)"
          >
            <div class="s-main">
              <div class="s-title" :title="s.title">{{ s.title }}</div>
              <div class="s-meta">{{ fmtAgo(s.updated_at) }} · {{ s.message_count }} 条</div>
            </div>
            <div class="s-actions">
              <a-tooltip title="改名">
                <a-button size="small" type="text" @click.stop="startRename(s)">
                  <EditOutlined />
                </a-button>
              </a-tooltip>
              <a-popconfirm
                title="删除这个会话及其全部消息？"
                ok-text="删除"
                cancel-text="取消"
                @confirm="onRemoveSession(s)"
              >
                <a-button size="small" type="text" danger @click.stop>
                  <DeleteOutlined />
                </a-button>
              </a-popconfirm>
            </div>
          </div>
          <div v-if="!sessions.length" class="side-empty">还没有会话，问一句就会自动创建</div>
        </div>
      </div>

      <a-collapse v-model:activeKey="paramsOpen" :bordered="false" class="params">
        <a-collapse-panel key="params" header="生成参数">
          <div class="param-row">
            <span class="param-label">温度</span>
            <span class="param-value">{{ form.temperature }}</span>
          </div>
          <a-slider v-model:value="form.temperature" :min="0" :max="2" :step="0.1" />
          <div class="param-row">
            <span class="param-label">最大生成长度</span>
            <span class="param-value">{{ form.maxTokens }} tokens</span>
          </div>
          <a-slider v-model:value="form.maxTokens" :min="256" :max="8192" :step="256" />
          <div class="param-row">
            <span class="param-label">联网检索</span>
          </div>
          <a-radio-group v-model:value="form.webMode" size="small" class="web-modes">
            <a-radio-button value="auto">自动</a-radio-button>
            <a-radio-button value="forced">必查</a-radio-button>
            <a-radio-button value="off">关闭</a-radio-button>
          </a-radio-group>
          <div class="hint">{{ webModeHint }}</div>
          <div class="param-row">
            <span class="param-label">仅输出 JSON</span>
            <a-switch v-model:checked="form.jsonMode" size="small" />
          </div>
          <!-- 互斥是后端定的：结构化输出优先，此时不挂工具（两者同时传会被兼容端点拒） -->
          <div v-if="form.jsonMode && form.webMode !== 'off'" class="hint warn">
            JSON 模式优先：本轮不会调用联网工具
          </div>
          <a-divider style="margin: 10px 0" />
          <div class="backend-info">
            <div><span class="muted">Provider</span>{{ backend?.name || '—' }}</div>
            <div><span class="muted">Chat 模型</span>{{ backend?.chat_model || '—' }}</div>
            <div><span class="muted">Embedding</span>{{ backend?.embedding_model || '—' }}</div>
          </div>
          <div class="hint">切换平台请改后端 .env 的 APP_LLM_DEFAULT_PROVIDER</div>
        </a-collapse-panel>
      </a-collapse>
    </aside>

    <!-- ---------- 右侧：对话流 + 输入 ---------- -->
    <section class="main">
      <header class="top-bar">
        <div class="top-title">
          {{ currentTitle }}
          <span class="muted">{{ backendDesc }}</span>
          <a-tag v-if="business.backendOk === false" color="orange">后端未响应</a-tag>
        </div>
        <div class="top-right">
          <span v-if="usageTotal" class="muted">本次累计 {{ usageTotal }} tokens</span>
          <a-button v-if="streaming" danger size="small" @click="onStop">
            <StopOutlined /> 停止生成
          </a-button>
        </div>
      </header>

      <div ref="listRef" class="chat-list">
        <!-- 空态：给几个能直接点的问题，别让人对着输入框发呆 -->
        <div v-if="!messages.length" class="hero">
          <div class="hero-title">想问点什么？</div>
          <div class="hero-sub">
            模型由后端配置决定；对话按登录身份保存，刷新或换设备都能接着聊。
          </div>
          <div class="samples">
            <a-tag v-for="s in samples" :key="s" color="blue" class="sample-tag" @click="useSample(s)">
              {{ s }}
            </a-tag>
          </div>
        </div>

        <template v-for="m in messages" :key="m.key">
          <!-- 用户提问：靠右 -->
          <div v-if="m.role === 'user'" class="msg-user">
            <div class="bubble">
              <div v-if="m.content" class="bubble-text">{{ m.content }}</div>
              <div v-if="m.images.length" class="bubble-imgs">
                <a-image
                  v-for="(im, i) in m.images"
                  :key="i"
                  :src="im"
                  :width="96"
                  :height="96"
                  class="bubble-img"
                />
              </div>
              <div v-if="m.imagesDropped" class="dropped-hint">
                {{ m.imagesDropped }} 张图片过大未持久化（回放时只保留文字）
              </div>
              <div class="bubble-meta">{{ fmtTime(m.createdAt) }}</div>
            </div>
          </div>

          <!-- 助手回答：靠左 -->
          <div v-else class="msg-ai">
            <div class="ai-card" :class="{ 'ai-error': !!m.error }">
              <!-- 联网过程先于答案展示：搜了几轮、查了什么词要看得见，否则用户只能对着一片空白等 -->
              <div v-if="m.webSteps?.length" class="web-steps">
                <div v-for="st in m.webSteps" :key="st.key" class="web-step">
                  <a-spin v-if="!st.done" size="small" />
                  <CheckCircleOutlined v-else-if="st.ok" class="step-ok" />
                  <CloseCircleOutlined v-else class="step-bad" />
                  <span class="step-detail">{{ st.detail }}</span>
                  <span v-if="!st.done" class="muted">检索中…</span>
                  <span v-else-if="st.ok" class="muted">
                    {{ st.sources.length ? `${st.sources.length} 条结果` : '无结果' }}
                  </span>
                  <span v-else class="step-bad-text">{{ st.error || '失败' }}</span>
                </div>
              </div>

              <div v-if="m.pending && !m.content" class="pending">
                <a-spin size="small" /> {{ m.webSteps?.length ? '正在整理搜索结果…' : '正在生成…' }}
              </div>
              <RichText v-else :text="m.content" :class="{ typing: m.pending }" />

              <!-- 思考过程：深度思考模型的推理内容，可折叠展示 -->
              <div v-if="m.thinking" class="thinking-block">
                <div class="thinking-header" @click="toggleThinking(m)">
                  <span class="muted">
                    <a-icon name="lightbulb" style="margin-right: 4px" />
                    思考过程（{{ m.thinking?.length || 0 }} 字）
                  </span>
                  <span class="muted">{{ isThinkingExpanded(m) ? '▼' : '▶' }}</span>
                </div>
                <div v-if="isThinkingExpanded(m)" class="thinking-content">
                  <RichText :text="m.thinking" />
                </div>
              </div>

              <div v-if="m.error" class="error-line">{{ m.error }}</div>
              <div v-else-if="m.stopped" class="muted stopped-line">（已在生成中途停止）</div>

              <!-- 链接只能来自工具返回，所以这里的 URL 是真实抓过的；新窗口打开要带 noopener -->
              <div v-if="m.sources?.length" class="web-sources">
                <div class="sources-title">参考来源（{{ m.sources.length }}）</div>
                <ol class="sources-list">
                  <li v-for="s in m.sources" :key="s.url">
                    <a :href="s.url" target="_blank" rel="noopener noreferrer">
                      {{ s.title || s.url }}
                    </a>
                    <div v-if="s.snippet" class="source-snippet">{{ s.snippet }}</div>
                  </li>
                </ol>
              </div>

              <div class="ai-foot">
                <span v-if="m.usage" class="muted">
                  提示 {{ m.usage.prompt_tokens }} + 生成 {{ m.usage.completion_tokens }} =
                  {{ m.usage.total_tokens }} tokens
                </span>
                <span v-else class="muted" />
                <a-space :size="4">
                  <a-button
                    v-if="m.content"
                    size="small"
                    type="text"
                    @click="copyText(m.content)"
                  >
                    <CopyOutlined /> 复制
                  </a-button>
                  <a-button
                    v-if="!m.pending && m.content"
                    size="small"
                    type="text"
                    title="保留上一版，再生成一版"
                    @click="onRegenerate"
                  >
                    <ReloadOutlined /> 再答一次
                  </a-button>
                </a-space>
              </div>
            </div>
          </div>
        </template>
      </div>

      <footer class="composer">
        <div v-if="pendingImages.length" class="attach-preview">
          <div v-for="(im, i) in pendingImages" :key="i" class="attach-thumb">
            <img :src="im" alt="待发送图片" />
            <span class="attach-close" @click="removeImage(i)">×</span>
          </div>
        </div>

        <a-textarea
          v-model:value="input"
          :auto-size="{ minRows: 2, maxRows: 8 }"
          :disabled="streaming"
          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
          @press-enter="onPressEnter"
        />

        <div class="composer-bar">
          <div class="attach-area">
            <label class="attach-btn" :class="{ disabled: streaming }">
              <input type="file" accept="image/*" multiple hidden @change="onPickImages" />
              <PaperClipOutlined /> 附图
            </label>
            <span class="hint">最多 {{ MAX_ATTACH }} 张，仅视觉模型可用</span>
          </div>
          <a-space>
            <a-button v-if="streaming" @click="onStop">
              <StopOutlined /> 停止
            </a-button>
            <a-button type="primary" :loading="streaming" @click="onSend">
              发送
            </a-button>
          </a-space>
        </div>
      </footer>
    </section>

    <a-modal
      v-model:open="renameOpen"
      title="会话改名"
      ok-text="保存"
      cancel-text="取消"
      @ok="doRename"
    >
      <a-input v-model:value="renameTitle" :maxlength="200" placeholder="给这个会话起个名字" />
    </a-modal>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  gap: 16px;
  height: calc(100vh - 128px);
  min-height: 560px;
}

/* ---------- 左侧栏 ---------- */
.side {
  width: 280px;
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--app-panel);
  border-radius: 8px;
  padding: 14px;
  overflow: hidden;
}
.side-block {
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.side-sessions {
  flex: 1;
  margin-top: 4px;
}
.side-title {
  font-weight: 600;
  color: var(--app-fg);
  font-size: 13px;
}
.session-list {
  flex: 1;
  overflow: auto;
  margin-top: 8px;
}
.session-item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 7px 6px;
  border-radius: 6px;
  cursor: pointer;
  min-width: 0;
}
.session-item:hover {
  background: var(--app-hover);
}
.session-item.active {
  background: var(--app-hover);
  box-shadow: inset 2px 0 0 #1677ff;
}
.s-main {
  flex: 1;
  min-width: 0;
}
.s-title {
  font-size: 13px;
  color: var(--app-fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.s-meta {
  font-size: 11px;
  color: var(--app-muted);
}
.s-actions {
  display: flex;
  flex: none;
  opacity: 0;
  transition: opacity 0.15s ease;
}
.session-item:hover .s-actions,
.session-item.active .s-actions {
  opacity: 1;
}
.side-empty {
  font-size: 12px;
  color: var(--app-muted);
  padding: 8px 2px;
}
.params {
  background: var(--app-panel);
}
.params :deep(.ant-collapse-header) {
  padding: 8px 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--app-fg);
}
.params :deep(.ant-collapse-content-box) {
  padding: 0 0 6px;
}
.param-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--app-muted);
}
.param-value {
  color: var(--app-fg);
  font-variant-numeric: tabular-nums;
}
.params :deep(.ant-slider) {
  margin: 6px 2px 12px;
}
.backend-info {
  font-size: 12px;
  color: var(--app-fg);
  display: grid;
  gap: 2px;
}
.backend-info .muted {
  display: inline-block;
  width: 76px;
}
.hint {
  margin-top: 6px;
  font-size: 11px;
  color: var(--app-muted);
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
  gap: 12px;
  margin-bottom: 8px;
}
.top-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 15px;
  color: var(--app-fg);
  min-width: 0;
}
.top-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: none;
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
.bubble-text {
  white-space: pre-wrap;
  word-break: break-word;
}
.bubble-imgs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.bubble-img {
  border-radius: 6px;
  object-fit: cover;
}
.bubble-img :deep(img) {
  border-radius: 6px;
  object-fit: cover;
}
.dropped-hint {
  margin-top: 6px;
  font-size: 11px;
  opacity: 0.85;
}
.bubble-meta {
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
.ai-card.ai-error {
  border-color: #ffa39e;
}
/* ---------- 联网检索过程与参考来源 ---------- */
.web-modes {
  margin-bottom: 4px;
}
.hint.warn {
  color: #d46b08;
}
.web-steps {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 8px;
  padding-bottom: 8px;
  border-bottom: 1px dashed var(--app-border);
}
.web-step {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--app-muted);
  min-width: 0;
}
.step-detail {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.step-ok {
  color: #52c41a;
}
.step-bad,
.step-bad-text {
  color: #cf1322;
}
.web-sources {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--app-border);
}
.sources-title {
  font-size: 12px;
  color: var(--app-muted);
  margin-bottom: 4px;
}
.sources-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
}
.sources-list li {
  margin-bottom: 4px;
  word-break: break-all;
}
.source-snippet {
  color: var(--app-muted);
  font-size: 11px;
  /* 摘要只作辨认用，最多两行，剩下的看原链接 */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.pending {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--app-muted);
  font-size: 13px;
}
/* 流式生成时在末尾挂一个光标，让“还在写”这件事一眼可见
   （typing 这个类经 attrs 透传落在 RichText 根节点上，所以直接叠用而不需 :deep） */
.typing::after {
  content: '▍';
  margin-left: 2px;
  color: #1677ff;
  animation: caret 1s steps(2, start) infinite;
}
@keyframes caret {
  to {
    visibility: hidden;
  }
}
.error-line {
  margin-top: 8px;
  font-size: 13px;
  color: #cf1322;
}
.stopped-line {
  margin-top: 6px;
}
.ai-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
}

/* ---------- 输入区 ---------- */
.composer {
  border-top: 1px solid var(--app-border);
  padding-top: 10px;
}
.attach-preview {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.attach-thumb {
  position: relative;
}
.attach-thumb img {
  width: 64px;
  height: 64px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid var(--app-border);
  display: block;
}
.attach-close {
  position: absolute;
  top: -6px;
  right: -6px;
  width: 18px;
  height: 18px;
  line-height: 16px;
  text-align: center;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  font-size: 14px;
  cursor: pointer;
}
.composer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
  gap: 12px;
}
.attach-area {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.attach-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  color: #1677ff;
  cursor: pointer;
  padding: 2px 8px;
  border: 1px solid var(--app-border);
  border-radius: 6px;
}
.attach-btn:hover {
  background: var(--app-hover);
}
.attach-btn.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 思考过程区块 */
.thinking-block {
  margin-top: 12px;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  overflow: hidden;
  background: var(--app-panel-bg, #fafafa);
}
.thinking-header {
  padding: 10px 12px;
  background: var(--app-hover);
  cursor: pointer;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  user-select: none;
  transition: background 0.2s ease;
}
.thinking-header:hover {
  background: rgba(22, 119, 255, 0.08);
}
.thinking-content {
  padding: 12px;
  border-top: 1px solid var(--app-border);
  max-height: 600px;
  overflow: auto;
}
.thinking-content :deep(.rich-text) {
  font-size: 13px;
  line-height: 1.6;
  color: var(--app-fg);
}

/* 窄屏：收起侧栏，专注对话（会话列表靠窄屏外的宽度使用） */
@media (max-width: 992px) {
  .side {
    display: none;
  }
}
</style>
