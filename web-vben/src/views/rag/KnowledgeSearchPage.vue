<script setup lang="ts">
/**
 * 知识库 · 检索问答 —— 流式多轮对话 + 引用来源。
 *   布局与 AI 对话页一致：消息气泡流式堆叠，每条 AI 回答内联「引用来源」，
 *      可与回答中的 [n] 对照；支持思考过程展示（如模型输出）。
 *  检索只覆盖「当前默认 provider 入库」的文档，stale 文档需到文档列表页重建后才命中。
 *  说明：answer 接口支持流式，会话列表把每次问答以对话形式呈现。
 */
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { Card, message } from 'ant-design-vue';
import { FileTextOutlined, PictureOutlined } from '@ant-design/icons-vue';
import { ragApi } from '@/api';
import { useBusinessStore } from '@/store/modules/business';
import type { KnowledgeDetail } from '@/typings';
import RichText from '@/components/RichText.vue';
import { fileToDataUrl, isImageFile } from '@/utils/rich';

type Mode = 'answer' | 'search';
interface UiMsg {
  key: string;
  role: 'user' | 'assistant';
  content: string;
  images: string[];
  citations?: any[];
  usage?: any;
  provider?: string;
  elapsed?: number;
  pending?: boolean;
  error?: string;
}

const business = useBusinessStore();
const router = useRouter();

const mode = ref<Mode>('answer');
const topK = ref(4);
const topKOptions = [2, 4, 6, 8, 12].map((k) => ({ label: `Top ${k}`, value: k }));
const modeOptions = [
  { label: '知识问答', value: 'answer' },
  { label: '相似检索', value: 'search' },
];

/* ---------------- 知识问答（对话） ---------------- */
const messages = ref<UiMsg[]>([]);
const input = ref('');
const streaming = ref(false);
const scroller = ref<HTMLElement | null>(null);
let controller: AbortController | null = null;
let keySeq = 0;
const mkKey = (): string => `m-${Date.now().toString(36)}-${(keySeq++).toString(36)}`;

// 随下一条提问附带的图片（data URL）
const pendingImages = ref<string[]>([]);
const MAX_ATTACH = 4;

async function onPickImages(ev: Event) {
  const inputEl = ev.target as HTMLInputElement;
  const picked = Array.from(inputEl.files || []).filter(isImageFile);
  inputEl.value = '';
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

function removeImage(index: number) {
  pendingImages.value.splice(index, 1);
}

const suggestions = [
  '这个知识库主要讲了什么？',
  '帮我总结一下核心要点',
  '有哪些常见问题及解答？',
];

const hasChat = computed(() => messages.value.length > 0);

function scrollBottom() {
  nextTick(() => {
    if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
  });
}
watch(() => messages.value.length, scrollBottom);

// 引用去重：同一文档可能命中多个片段，只保留一个可点击的标题。
function citedDocs(msg: UiMsg): { id: string; title: string }[] {
  const seen = new Map<string, string>();
  for (const c of msg.citations || []) {
    const id = c.knowledge_id;
    if (id && !seen.has(id)) seen.set(id, c.source || id);
  }
  return [...seen.entries()].map(([id, title]) => ({ id, title }));
}

// 汇总一条回答命中文档里的图片（去重）
function collectImages(msg: UiMsg): string[] {
  const seen = new Set<string>();
  const urls: string[] = [];
  for (const c of msg.citations || []) {
    for (const u of c.images || []) {
      if (u && !seen.has(u)) {
        seen.add(u);
        urls.push(u);
      }
    }
  }
  return urls;
}

/* ---------------- 发送 / 停止 ---------------- */
async function onSend(): Promise<void> {
  const question = input.value.trim();
  const images = [...pendingImages.value];
  if (!question && !images.length) {
    message.warning('请输入问题或附加图片');
    return;
  }
  if (streaming.value) return;

  // 添加用户消息
  messages.value.push({
    key: mkKey(),
    role: 'user',
    content: question,
    images,
    citations: [],
    usage: null,
    provider: undefined,
    elapsed: undefined,
  });
  input.value = '';
  pendingImages.value = [];
  scrollBottom();

  // 准备助手消息占位
  const reply: UiMsg = {
    key: mkKey(),
    role: 'assistant',
    content: '',
    images: [],
    citations: [],
    usage: null,
    provider: undefined,
    elapsed: Date.now(),
    pending: true,
  };
  messages.value.push(reply);
  const live = messages.value[messages.value.length - 1];

  controller = new AbortController();
  streaming.value = true;
  let failure = '';
  let aborted = false;

  try {
    // 使用流式 API
    const res = await ragApi.answerStream(
      {
        query: question,
        top_k: topK.value,
        images: images.length ? images : undefined,
        stream: true,
      },
      controller.signal,
    );

    const reader = res.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      const chunk = decoder.decode(value, { stream: true });
      const lines = chunk.split('\n').filter((line) => line.trim());

      for (const line of lines) {
        try {
          const data = JSON.parse(line);
          if (data.error) {
            failure = data.error;
            break;
          }
          if (data.answer) {
            live.content += data.answer;
          }
          if (data.citations && data.citations.length) {
            live.citations = data.citations;
          }
          if (data.usage) {
            live.usage = data.usage;
          }
          if (data.provider) {
            live.provider = data.provider;
          }
        } catch {
          // 忽略解析错误
        }
      }
      scrollTick();
    }
  } catch (e: any) {
    if (e?.name === 'AbortError') {
      aborted = true;
    } else {
      failure = e?.detail || e?.message || '问答失败';
    }
  } finally {
    live.pending = false;
    streaming.value = false;
    controller = null;
  }

  if (failure) {
    live.error = failure;
    message.error(failure);
    return;
  }
  if (!live.content && !aborted) {
    live.error = '未生成有效回答（可能是知识库为空或文档需重建索引）';
    message.warning(live.error);
    return;
  }
  if (aborted) {
    live.provider = undefined; // 标记已停止
  }
  live.elapsed = Date.now() - live.elapsed!;
}

function onStop(): void {
  controller?.abort();
}

function clearChat(): void {
  messages.value = [];
}

function onPressEnter(ev: KeyboardEvent): void {
  if (ev.shiftKey) return;
  ev.preventDefault();
  void onSend();
}

// 滚动计数器
let scrollTickCount = 0;
function scrollTick() {
  scrollTickCount += 1;
  nextTick(() => {
    if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
  });
}

/* ---------------- 文档详情抽屉（当前页直接打开，不跳走） ---------------- */
const docDrawer = reactive({
  visible: false,
  loading: false,
  id: '',
  data: null as KnowledgeDetail | null,
});

async function openDoc(id: string) {
  docDrawer.visible = true;
  docDrawer.loading = true;
  docDrawer.id = id;
  docDrawer.data = null;
  try {
    // 只读预览：正文取 doc.content，分片拉 1 条即可（避免拉全部分片）。
    docDrawer.data = await ragApi.detail(id, 1);
  } catch (e: any) {
    message.error(e?.detail || e?.message || '加载文档详情失败');
    docDrawer.visible = false;
  } finally {
    docDrawer.loading = false;
  }
}

// 需要改名 / 编辑 / 重建时，再带到文档列表页（并自动展开该文档）。
function goManage(id: string) {
  router.push({ path: '/knowledge', query: { doc: id } });
}

const kindLabels: Record<string, string> = {
  file: '文件',
  text: '文本',
  faq: '问答对',
  url: '网页',
};
// 引用文档的向量化状态中文标签（详情抽屉展示）
const docStatusLabels: Record<string, string> = {
  indexed: '已学习',
  pending: '待重建',
  processing: '处理中',
  failed: '重建失败',
};
function fmtDate(ts: number): string {
  return ts ? new Date(ts * 1000).toLocaleString() : '—';
}

/* ---------------- 相似检索 ---------------- */
const query = ref('');
const searching = ref(false);
const elapsed = ref(0);
const providerUsed = ref('');
const hits = ref<any[]>([]);
const searched = ref(false);

function scorePercent(score: number): number {
  const p = Math.round((Number(score) || 0) * 100);
  return Math.max(0, Math.min(100, p));
}
function scoreColor(score: number): string {
  const s = Number(score) || 0;
  if (s >= 0.6) return '#52c41a';
  if (s >= 0.4) return '#1677ff';
  return '#d9d9d9';
}

async function runSearch() {
  const q = query.value.trim();
  if (!q) return message.warning('请输入查询');
  if (searching.value) return;
  searching.value = true;
  const started = Date.now();
  try {
    const res = await ragApi.search({ query: q, top_k: topK.value });
    const data = (res as any)?.data ?? res;
    hits.value = data?.items || [];
    providerUsed.value = data?.provider || '';
    searched.value = true;
    elapsed.value = Date.now() - started;
    if (!hits.value.length) message.warning('未命中相似片段（可能是文档需重建索引或不属于当前 provider）');
  } catch (e: any) {
    message.error(e?.detail || e?.message || '检索失败');
  } finally {
    searching.value = false;
  }
}

// 模式或 Top K 变化时，滚动到对话底部，保持输入焦点体验
watch(mode, () => scrollBottom());

onMounted(() => {
  business.loadProviders();
  business.pollHealth();
});
</script>

<template>
  <div class="page-card rag-page">
    <a-alert v-if="!business.backendOk" type="warning" show-icon style="margin-bottom: 12px"
      message="后端当前未响应（/health 未通过），检索与问答可能失败" />

    <Card :bordered="false" class="rag-console" :body-style="{ padding: '0' }">
      <template #title>
        <a-space>
          <span>知识库助手</span>
          <a-tag color="blue">{{ business.activeProvider?.name || '后端配置' }}</a-tag>
        </a-space>
      </template>
      <template #extra>
        <a-radio-group
          v-model:value="mode"
          :options="modeOptions"
          option-type="button"
          button-style="solid"
        />
      </template>

      <!-- ========== 知识问答：对话式 ========== -->
      <div v-if="mode === 'answer'" class="chat-wrap">
        <div ref="scroller" class="chat-body">
          <!-- 欢迎 / 空态 -->
          <div v-if="!hasChat" class="chat-welcome">
            <div class="welcome-avatar">🤖</div>
            <div class="welcome-title">向知识库提问</div>
            <div class="welcome-sub">回答会基于知识库检索到的片段生成，并附引用来源。试试这些：</div>
            <div class="welcome-sugs">
              <a-button v-for="(s, i) in suggestions" :key="i" size="small" @click="input = s; onPressEnter({ preventDefault: () => {} } as KeyboardEvent)">{{ s }}</a-button>
            </div>
          </div>

          <!-- 消息流 -->
          <div v-for="m in messages" :key="m.key" class="msg-row">
            <!-- 用户 -->
            <div v-if="m.role === 'user'" class="msg user">
              <div class="bubble user-bubble">
                <div class="user-text">{{ m.content }}</div>
                <div v-if="m.images && m.images.length" class="user-imgs">
                  <img v-for="(img, i) in m.images" :key="i" :src="img" class="user-img" />
                </div>
              </div>
              <div class="avatar user-avatar">我</div>
            </div>

            <!-- AI -->
            <div v-else class="msg ai">
              <div class="avatar ai-avatar">AI</div>
              <div class="bubble ai-bubble">
                <a-spin v-if="m.pending" :spinning="true" size="small">
                  <span class="typing">正在检索知识库并生成回答<span class="dots">…</span></span>
                </a-spin>
                <template v-else>
                  <div v-if="m.error" class="ai-error">{{ m.error }}</div>
                  <RichText v-else class="answer-text" :text="m.content || ''" />

                  <!-- 检索到的图片：来自命中文档（图片入库 / 文档内嵌图），可点击预览 -->
                  <div v-if="collectImages(m).length" class="kb-imgs">
                    <div class="kb-imgs-label">相关图片 {{ collectImages(m).length }}</div>
                    <div class="kb-imgs-grid">
                      <a-image
                        v-for="(u, i) in collectImages(m)"
                        :key="i"
                        :src="u"
                        :width="112"
                        :height="112"
                        class="kb-img"
                      />
                    </div>
                  </div>

                  <!-- 元信息 -->
                  <div class="answer-meta">
                    <a-tag v-if="m.provider" color="geekblue">{{ m.provider }}</a-tag>
                    <a-tag v-if="m.elapsed">{{ m.elapsed }} ms</a-tag>
                    <a-tag v-if="m.usage">token {{ m.usage.total_tokens }}</a-tag>
                  </div>

                  <!-- 引用来源：仅展示去重后的文档标题，点击在当前页弹出详情抽屉 -->
                  <div v-if="citedDocs(m).length" class="cite-box">
                    <span class="cite-label">参考文档</span>
                    <a-tag
                      v-for="d in citedDocs(m)"
                      :key="d.id"
                      color="blue"
                      class="cite-doc"
                      @click="openDoc(d.id)"
                    >
                      <FileTextOutlined /> {{ d.title }}
                    </a-tag>
                  </div>
                </template>
              </div>
            </div>
          </div>
        </div>

        <!-- 输入区 -->
        <div class="chat-composer">
          <div class="composer-top">
            <a-select v-model:value="topK" :options="topKOptions" size="small" style="width: 100px" />
            <label class="attach-btn" :class="{ disabled: streaming || pendingImages.length >= MAX_ATTACH }">
              <input type="file" accept="image/*" multiple hidden @change="onPickImages" />
              <PictureOutlined /> 附图
            </label>
            <span class="attach-hint">图片需后端配置支持视觉的对话模型才能被识别</span>
          </div>
          <div v-if="pendingImages.length" class="attach-preview">
            <div v-for="(img, i) in pendingImages" :key="i" class="attach-thumb">
              <img :src="img" />
              <span class="attach-close" @click="removeImage(i)">×</span>
            </div>
          </div>
          <div class="composer-input">
            <a-textarea
              v-model:value="input"
              :auto-size="{ minRows: 1, maxRows: 5 }"
              placeholder="输入问题，回车发送（Shift+Enter 换行）"
              :disabled="streaming"
              @press-enter="onPressEnter"
            />
            <a-button v-if="streaming" type="primary" @click="onStop">停止</a-button>
            <a-button type="primary" :loading="streaming" :disabled="!input.trim()" @click="onSend">发送</a-button>
            <a-button v-if="hasChat" :disabled="streaming" @click="clearChat">清空</a-button>
          </div>
        </div>
      </div>

      <!-- ========== 相似检索 ========== -->
      <div v-else class="search-wrap">
        <a-space wrap style="margin-bottom: 12px">
          <a-input-search
            v-model:value="query"
            placeholder="输入查询，查看命中的相似片段"
            style="width: min(320px, 100%)"
            enter-button="检索"
            :loading="searching"
            @search="runSearch"
          />
          <a-select v-model:value="topK" :options="topKOptions" style="width: 110px" />
        </a-space>

        <template v-if="searched">
          <a-space wrap style="margin-bottom: 8px">
            <a-tag color="blue">命中 {{ hits.length }} 个片段</a-tag>
            <a-tag v-if="providerUsed">provider：{{ providerUsed }}</a-tag>
            <a-tag v-if="elapsed">耗时 {{ elapsed }} ms</a-tag>
          </a-space>
          <a-empty v-if="!hits.length" description="没有命中，可能是文档需重建索引或知识库为空" />
          <Card v-for="c in hits" :key="c.index" :bordered="false" size="small" class="hit-card">
            <div class="hit-head">
              <a-tag class="rank">[#{{ c.index + 1 }}]</a-tag>
              <span class="hit-source">{{ c.source }}</span>
              <span class="hit-score">{{ (Number(c.score) || 0).toFixed(4) }}</span>
            </div>
            <div v-if="c.images && c.images.length" class="hit-imgs">
              <a-image
                v-for="(u, i) in c.images"
                :key="i"
                :src="u"
                :width="88"
                :height="88"
                class="hit-img"
              />
            </div>
            <a-progress :percent="scorePercent(c.score)" :stroke-color="scoreColor(c.score)"
              :show-info="false" size="small" style="margin: 6px 0" />
            <div class="snippet">{{ c.snippet }}</div>
          </Card>
        </template>
        <a-empty v-else description="输入查询并点「检索」，查看命中的相似片段" />
      </div>
    </Card>

    <!-- 文档详情抽屉：在当前页只读预览引用文档，不必离开问答页 -->
    <a-drawer
      :open="docDrawer.visible"
      :title="docDrawer.data?.doc?.title || '文档详情'"
      width="min(720px, 94vw)"
      @close="docDrawer.visible = false"
    >
      <a-spin :spinning="docDrawer.loading">
        <template v-if="docDrawer.data">
          <a-descriptions :column="2" size="small" bordered>
            <a-descriptions-item label="ID" :span="2">{{ docDrawer.data.doc.knowledge_id }}</a-descriptions-item>
            <a-descriptions-item label="状态">
              <a-tag :color="docDrawer.data.doc.status === 'indexed' ? 'green' : 'orange'">
                {{ docStatusLabels[docDrawer.data.doc.status] || docDrawer.data.doc.status }}
              </a-tag>
            </a-descriptions-item>
            <a-descriptions-item label="录入方式">{{ kindLabels[docDrawer.data.doc.kind] || docDrawer.data.doc.kind }}</a-descriptions-item>
            <a-descriptions-item label="分片数">{{ docDrawer.data.chunk_total }}</a-descriptions-item>
            <a-descriptions-item label="字符数">{{ (docDrawer.data.doc.chars || 0).toLocaleString() }}</a-descriptions-item>
            <a-descriptions-item label="更新时间" :span="2">{{ fmtDate(docDrawer.data.doc.updated_at) }}</a-descriptions-item>
            <a-descriptions-item v-if="docDrawer.data.doc.source_file" label="来源文件" :span="2">
              {{ docDrawer.data.doc.source_file }}
            </a-descriptions-item>
          </a-descriptions>

          <a-alert
            v-if="docDrawer.data.doc.status !== 'indexed'"
            type="warning"
            show-icon
            style="margin-top: 12px"
            message="该文档尚未按当前嵌入模型完成向量化，检索不会命中，请到文档列表重建索引"
          />

          <template v-if="docDrawer.data.doc.metadata?.images?.length">
            <div class="drawer-section">
              内嵌图片（{{ docDrawer.data.doc.metadata.images.length }}
              <template v-if="docDrawer.data.doc.metadata.embedded_images > docDrawer.data.doc.metadata.images.length"> / 文档共 {{ docDrawer.data.doc.metadata.embedded_images }} 张，超出上限部分仅保留描述</template>）
            </div>
            <div class="drawer-imgs">
              <a-image
                v-for="(im, i) in docDrawer.data.doc.metadata.images"
                :key="i"
                :src="im"
                :width="120"
                :height="120"
                class="drawer-img"
              />
            </div>
          </template>

          <div class="drawer-section">正文</div>
          <RichText
            v-if="docDrawer.data.content"
            class="drawer-content"
            :text="docDrawer.data.content"
          />
          <a-empty
            v-else
            :description="docDrawer.data.doc.has_content ? '暂无正文' : '入库时未保存原文，只能由有 knowledge:manage 的账号重新上传'"
          />
        </template>
        <a-empty v-else-if="!docDrawer.loading" description="没有加载到文档" />
      </a-spin>
      <template #footer>
        <a-space>
          <a-button @click="docDrawer.visible = false">关闭</a-button>
          <a-button type="primary" @click="goManage(docDrawer.id)">到文档列表管理</a-button>
        </a-space>
      </template>
    </a-drawer>
  </div>
</template>

<style scoped>
.rag-page { display: flex; flex-direction: column; }
.rag-console { display: flex; flex-direction: column; }
.rag-console :deep(.ant-card-body) { flex: 1; display: flex; flex-direction: column; min-height: 0; }

/* ---- 对话 ---- */
.chat-wrap { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.chat-body {
  flex: 1;
  overflow-y: auto;
  height: 56vh;
  min-height: 280px;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  background: var(--app-panel-2);
}
.chat-welcome {
  margin: auto;
  text-align: center;
  max-width: 520px;
  color: var(--app-muted);
}
.welcome-avatar { font-size: 40px; }
.welcome-title { font-size: 18px; font-weight: 600; color: var(--app-fg); margin: 8px 0 4px; }
.welcome-sub { font-size: 13px; margin-bottom: 12px; }
.welcome-sugs { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }

.msg-row { display: flex; flex-direction: column; gap: 8px; }
.msg { display: flex; align-items: flex-start; gap: 10px; }
.msg.user { justify-content: flex-end; }
.msg.ai { justify-content: flex-start; }
.avatar {
  width: 32px; height: 32px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700; flex-shrink: 0;
}
.user-avatar { background: #2563eb; color: #fff; }
.ai-avatar { background: #16a34a; color: #fff; }
.bubble {
  max-width: 76%;
  padding: 10px 14px;
  border-radius: 12px;
  line-height: 1.6;
  word-break: break-word;
}
.user-bubble {
  background: #2563eb; color: #fff;
  border-bottom-right-radius: 3px;
}
.user-text { white-space: pre-wrap; }
.user-imgs { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.user-img {
  max-width: 160px; max-height: 160px; border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.5);
}
.ai-bubble {
  background: var(--app-panel); color: var(--app-fg);
  border: 1px solid var(--app-border); border-bottom-left-radius: 3px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
}
.answer-text { font-size: 14px; }
.ai-error { color: #dc2626; font-size: 13px; }
.typing { color: var(--app-muted); font-size: 13px; padding-left: 8px; }
.dots { animation: blink 1.2s infinite; }
@keyframes blink { 0%, 100% { opacity: 0.2; } 50% { opacity: 1; } }
/* 流式生成时在末尾挂一个光标，让“还在写”这件事一眼可见 */
.typing::after {
  content: '▍';
  margin-left: 2px;
  color: #1677ff;
  animation: caret 1s steps(2, start) infinite;
}
@keyframes caret {
  to { visibility: hidden; }
}
.answer-meta { margin-top: 10px; display: flex; gap: 6px; flex-wrap: wrap; }
.cite-box {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed #eef0f2;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.cite-label { font-size: 12px; color: var(--app-muted); }
.cite-doc {
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
}
.cite-doc:hover { opacity: 0.8; }
.cite-doc :deep(.anticon) { font-size: 12px; }

/* 回答内联的检索图片画廊 */
.kb-imgs { margin-top: 10px; }
.kb-imgs-label { font-size: 12px; color: var(--app-muted); margin-bottom: 6px; }
.kb-imgs-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.kb-img :deep(.ant-image-img) {
  object-fit: cover; border-radius: 8px; border: 1px solid var(--app-border);
}

.chat-composer {
  display: flex; flex-direction: column; gap: 8px;
  padding: 12px 18px; border-top: 1px solid var(--app-border); background: var(--app-panel);
}
.chat-composer :deep(.ant-input) { font-size: 14px; }
.composer-top { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.composer-input { display: flex; align-items: flex-end; flex-wrap: wrap; gap: 8px; }
.attach-btn {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 13px; color: #2563eb; cursor: pointer;
  padding: 2px 8px; border: 1px solid #d6e4ff; border-radius: 6px;
}
.attach-btn:hover { background: var(--app-hover); }
.attach-btn.disabled { opacity: 0.5; cursor: not-allowed; }
.attach-hint { font-size: 12px; color: var(--app-muted); min-width: 120px; }
.attach-preview { display: flex; flex-wrap: wrap; gap: 8px; }
.attach-thumb { position: relative; }
.attach-thumb img {
  width: 64px; height: 64px; object-fit: cover; border-radius: 6px;
  border: 1px solid var(--app-border);
}
.attach-close {
  position: absolute; top: -6px; right: -6px; width: 18px; height: 18px;
  line-height: 16px; text-align: center; border-radius: 50%;
  background: rgba(0, 0, 0, 0.55); color: #fff; font-size: 14px; cursor: pointer;
}

/* ---- 检索 ---- */
.search-wrap { padding: 16px 18px; overflow-y: auto; }
.hit-card { margin-bottom: 10px; }
.hit-imgs { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
.hit-img :deep(.ant-image-img) {
  object-fit: cover; border-radius: 6px; border: 1px solid var(--app-border);
}
.hit-head { display: flex; align-items: center; gap: 8px; }
.hit-source {
  flex: 1; font-weight: 600; color: var(--app-fg);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.hit-score { font-family: 'SF Mono', Consolas, monospace; font-size: 12px; color: var(--app-muted); }
.snippet {
  font-size: 13px; color: var(--app-fg); line-height: 1.6;
  white-space: pre-wrap; word-break: break-word;
  background: var(--app-panel-2); border-radius: 6px; padding: 8px 10px;
}

/* ---- 文档详情抽屉 ---- */
.drawer-section { font-size: 14px; font-weight: 600; color: var(--app-fg); margin: 16px 0 8px; }
.drawer-imgs { display: flex; flex-wrap: wrap; gap: 8px; }
.drawer-img :deep(.ant-image-img) {
  object-fit: cover; border-radius: 6px; border: 1px solid var(--app-border);
}
.drawer-content { font-size: 14px; line-height: 1.7; color: var(--app-fg); }
</style>
