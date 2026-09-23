<script setup lang="ts">
/**
 * 知识库 · 内容录入 —— 四种入库方式（文件多选 / 文本 / 问答对 / URL）。
 * 版式：左侧竖排「方式导航」+ 右侧工作区（头部说明 / 字段区 / 底部固定操作条）。
 * 向量化模型由后端 .env 配置决定，前端不选择平台；入库后可跳转「文档列表」查看与检索。
 *
 * 文本录入带格式选择（纯文本 / Markdown / HTML）：编辑器只存源码，HTML 由后端转成
 * Markdown（见 app/services/markdown_text.py），所以入库后三种格式都归一成了同一种正文。
 *
 * 样式全部走 global.css 的 --app-* 变量（明暗两套都能看），只用 #2563eb 这一个品牌色
 * 做「当前方式」的描边；不再往模板里塞内联 style。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import { message } from 'ant-design-vue';
import {
  AlignLeftOutlined,
  CloseOutlined,
  CloudUploadOutlined,
  FileTextOutlined,
  GlobalOutlined,
  QuestionCircleOutlined,
} from '@ant-design/icons-vue';
import { ragApi, uploadApi } from '@/api';
import MarkdownEditor from '@/components/MarkdownEditor.vue';
import { useBusinessStore } from '@/store/modules/business';
import type { ContentFormat, KnowledgePair, UploadBatchItem } from '@/typings';

const router = useRouter();
const business = useBusinessStore();

type AddMode = 'file' | 'text' | 'pairs' | 'url';

const allowedExts = ['pdf', 'docx', 'txt', 'md', 'csv', 'log', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'];
const supportedFormats = '支持 PDF / DOCX / TXT / MD / CSV / LOG，以及图片 PNG / JPG / GIF / WEBP / BMP';
const MAX_BATCH_FILES = 20;

/** 左侧导航一项 = 图标 + 名称 + 一句话适用场景；hint 是右栏头部的展开说明 */
const modeItems: {
  value: AddMode;
  label: string;
  desc: string;
  hint: string;
  icon: any;
}[] = [
  {
    value: 'file',
    label: '上传文件',
    desc: '已有文档，批量入库',
    hint: '多选时逐文件入库，标题取文件名主干（同名覆盖）；.md 文件会按 Markdown 记录格式',
    icon: FileTextOutlined,
  },
  {
    value: 'text',
    label: '文本录入',
    desc: 'Markdown / 富文本 / 纯文本',
    hint: '正文只存一种格式：Markdown 源码，或按原样保存的纯文本；HTML 会在入库时转成 Markdown',
    icon: AlignLeftOutlined,
  },
  {
    value: 'pairs',
    label: '问答对',
    desc: '客服口径、短条规则',
    hint: '一批问答对合成一篇 FAQ 文档，分片长度建议 800 以上，让一条问答对尽量落在同一分片里',
    icon: QuestionCircleOutlined,
  },
  {
    value: 'url',
    label: 'URL 抓取',
    desc: '在线文档 / 公告页',
    hint: '后端抓取网页并剔除导航与脚本，只留正文入库；动态渲染或需要登录的页面抓不到内容',
    icon: GlobalOutlined,
  },
];

const mode = ref<AddMode>('file');
const currentMode = computed(() => modeItems.find((m) => m.value === mode.value)!);

const form = reactive({
  title: '',
  content: '',
  /** 正文格式：默认 Markdown，纯文本与 HTML 都在下拉里显式切换 */
  format: 'markdown' as ContentFormat,
  chunkSize: 500,
  url: '',
  urlTitle: '',
  maxChars: 200_000,
});

const formatOptions: { label: string; value: ContentFormat }[] = [
  { label: '纯文本', value: 'text' },
  { label: 'Markdown', value: 'markdown' },
  { label: 'HTML 富文本', value: 'html' },
];
const formatHint = computed(() => {
  if (form.format === 'text') return '按原样入库，详情里也不解析语法符号';
  if (form.format === 'html') return '从网页 / Word 粘贴，入库时后端转成 Markdown（标题、列表、表格都保留）';
  return '工具栏与插图可用，正文只存 Markdown 源码';
});

const files = ref<File[]>([]);
const pairs = ref<KnowledgePair[]>([{ question: '', answer: '' }]);
const pairPaste = ref('');
const submitting = ref(false);
// 批量上传部分失败时留在页面逐条展示，不直接清掉
const batchResults = ref<UploadBatchItem[]>([]);

/* ---------------- 拖拽选择文件 ---------------- */
const fileInput = ref<HTMLInputElement | null>(null);
const dragOver = ref(false);
const acceptAttr = computed(() => allowedExts.map((e) => `.${e}`).join(','));

function switchMode(next: AddMode) {
  if (mode.value === next) return;
  mode.value = next;
  onModeChange();
}

// 切换录入方式时清掉与上一方式相关的临时输入，避免串味
function onModeChange() {
  form.title = '';
  form.content = '';
  form.format = 'markdown';
  form.url = '';
  form.urlTitle = '';
  files.value = [];
  pairs.value = [{ question: '', answer: '' }];
  pairPaste.value = '';
  batchResults.value = [];
}

function resetAll() {
  form.title = '';
  form.content = '';
  form.format = 'markdown';
  form.chunkSize = 500;
  form.url = '';
  form.urlTitle = '';
  files.value = [];
  pairs.value = [{ question: '', answer: '' }];
  pairPaste.value = '';
}

function fileExt(name: string): string {
  return name.split('.').pop()?.toLowerCase() || '';
}

/** 统一的入队口：过滤不支持的扩展名 + 截断到单次上限（点击选与拖拽选共用） */
function acceptFiles(picked: File[]) {
  const good = picked.filter((f) => allowedExts.includes(fileExt(f.name)));
  if (good.length !== picked.length) {
    message.warning(`已忽略 ${picked.length - good.length} 个不支持的文件，${supportedFormats}`);
  }
  let next = good;
  if (files.value.length) {
    // 已经在列表里的不重复加（同一个文件再拖一次很常见）
    const seen = new Set(files.value.map((f) => `${f.name}:${f.size}`));
    next = next.filter((f) => !seen.has(`${f.name}:${f.size}`));
  }
  if (!next.length) return message.info('没有新增可选的文件');
  const merged = [...files.value, ...next];
  if (merged.length > MAX_BATCH_FILES) {
    message.warning(`一次最多 ${MAX_BATCH_FILES} 个，多余的已忽略`);
  }
  files.value = merged.slice(0, MAX_BATCH_FILES);
  batchResults.value = [];
  // 单文件时允许自定义标题；多选时标题只能取文件名（一个请求对应多个文档）
  if (files.value.length === 1 && !form.title) {
    form.title = files.value[0].name.replace(/\.[^.]+$/, '');
  }
}

function onPickFile(ev: Event) {
  const input = ev.target as HTMLInputElement;
  const picked = Array.from(input.files || []);
  input.value = ''; // 同一个文件再选一次也要能触发 change
  acceptFiles(picked);
}

function onDrop(ev: DragEvent) {
  dragOver.value = false;
  acceptFiles(Array.from(ev.dataTransfer?.files || []));
}

/**
 * 拖拽经过子元素（图标 / 文案）时浏览器也会报 dragleave，直接置 false 会抖。
 * 只有真正离开整块区域才收起高亮。
 */
function onDragLeave(ev: DragEvent) {
  const zone = ev.currentTarget as HTMLElement | null;
  if (zone && ev.relatedTarget && zone.contains(ev.relatedTarget as Node)) return;
  dragOver.value = false;
}

function removeFile(index: number) {
  files.value.splice(index, 1);
  batchResults.value = [];
}

function clearFiles() {
  files.value = [];
  batchResults.value = [];
}

function fmtSize(bytes: number): string {
  if (!bytes) return '0 KB';
  if (bytes < 1024 * 100) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function extLabel(name: string): string {
  return (fileExt(name) || 'file').toUpperCase().slice(0, 4);
}

const totalBytes = computed(() => files.value.reduce((sum, f) => sum + f.size, 0));

/* ---------------- 问答对 ---------------- */
const validPairs = computed(() =>
  pairs.value
    .map((p) => ({ question: p.question.trim(), answer: p.answer.trim() }))
    .filter((p) => p.question && p.answer),
);
const droppedPairs = computed(() => pairs.value.length - validPairs.value.length);

function addPairRow() {
  pairs.value.push({ question: '', answer: '' });
}

function removePairRow(index: number) {
  if (pairs.value.length === 1) {
    pairs.value[0] = { question: '', answer: '' };
    return;
  }
  pairs.value.splice(index, 1);
}

// 粘贴格式：每行「问题 | 答案」，也兼容从 Excel 直接粘的 Tab 分隔
function parsePastedPairs(): KnowledgePair[] {
  return pairPaste.value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const hit = line.match(/\t|\s*\|\s*/);
      if (!hit || hit.index === undefined) return { question: line, answer: '' };
      return {
        question: line.slice(0, hit.index).trim(),
        answer: line.slice(hit.index + hit[0].length).trim(),
      };
    })
    .filter((p) => p.question && p.answer);
}

function onApplyPaste() {
  const parsed = parsePastedPairs();
  if (!parsed.length) return message.warning('没解析出问答对，请用「问题 | 答案」每行一条');
  const filled = pairs.value.filter((p) => p.question.trim() || p.answer.trim());
  pairs.value = [...filled, ...parsed];
  pairPaste.value = '';
  message.success(`已追加 ${parsed.length} 条问答对`);
}

/* ---------------- 提交 ---------------- */
const batchErrors = computed(() => batchResults.value.filter((r) => r.status === 'error'));

function summarizeBatch(results: UploadBatchItem[]) {
  batchResults.value = results;
  const ok = results.filter((r) => r.status === 'success');
  const bad = results.filter((r) => r.status === 'error');
  if (ok.length) message.success(`已入库 ${ok.length} 个文件`);
  if (bad.length) message.warning(`${bad.length} 个文件未入库，详见下方列表`);
}

function goDocs() {
  router.push('/knowledge');
}

async function onSubmit() {
  if (submitting.value) return;
  const title = form.title.trim();

  if (mode.value === 'file') {
    if (!files.value.length) return message.warning('请选择要上传的文件');
  } else if (!title) {
    return message.warning('请填写文档标题');
  }
  if (mode.value === 'text' && !form.content.trim()) return message.warning('请填写正文内容');
  if (mode.value === 'pairs' && !validPairs.value.length) {
    return message.warning('至少填写一条完整的问答对（问题与答案都不能为空）');
  }
  if (mode.value === 'url' && !/^https?:\/\//i.test(form.url.trim())) {
    return message.warning('请填写以 http:// 或 https:// 开头的完整地址');
  }

  submitting.value = true;
  try {
    if (mode.value === 'file') {
      if (files.value.length === 1) {
        const only = files.value[0];
        const res: any = await uploadApi.uploadFile(
          only,
          title || only.name.replace(/\.[^.]+$/, ''),
          form.chunkSize,
        );
        const data = (res?.data ?? res) as any;
        if (!data || data.status === 'error') throw new Error(data?.error || '解析入库失败');
        message.success(`已入库，共 ${data.chunks || 0} 个分片 / ${data.chars || 0} 字`);
      } else {
        const result = await uploadApi.uploadBatch(files.value, form.chunkSize);
        summarizeBatch(result?.results || []);
        if (result?.succeeded) business.pollHealth();
        if (!result?.failed) resetAll();
        return;
      }
    } else if (mode.value === 'text') {
      const data = await ragApi.ingest({
        title,
        content: form.content,
        format: form.format,
        chunk_size: form.chunkSize,
      });
      message.success(`已入库，共 ${data.chunks} 个分片 / ${data.chars} 字`);
    } else if (mode.value === 'pairs') {
      const data = await ragApi.ingestPairs({
        title,
        pairs: validPairs.value,
        chunk_size: form.chunkSize,
      });
      message.success(
        `已录入 ${validPairs.value.length} 条问答对，切为 ${data.chunks} 个分片`
          + (droppedPairs.value ? `（${droppedPairs.value} 条不完整已忽略）` : ''),
      );
    } else {
      const data = await ragApi.fetchUrl({
        url: form.url.trim(),
        title: form.urlTitle.trim() || undefined,
        chunk_size: form.chunkSize,
        max_chars: form.maxChars,
      });
      message.success(
        `已抓取《${data.title}》，${data.chars} 字 / ${data.chunks} 个分片`
          + (data.truncated ? '（超长已截断）' : ''),
      );
    }
    resetAll();
  } catch (e: any) {
    message.error(e?.detail || e?.message || '入库失败');
  } finally {
    submitting.value = false;
  }
}

onMounted(() => {
  business.pollHealth();
});
</script>

<template>
  <div class="page-card ingest-page">
    <a-alert
      v-if="!business.backendOk"
      class="warn-alert"
      type="warning"
      show-icon
      message="后端当前未响应（/health 未通过），入库可能失败"
    />

    <div class="ingest-shell">
      <!-- ========== 左：录入方式导航 ========== -->
      <aside class="ingest-nav">
        <div class="nav-caption">录入方式</div>
        <button
          v-for="item in modeItems"
          :key="item.value"
          class="nav-item"
          :class="{ 'is-active': mode === item.value }"
          type="button"
          @click="switchMode(item.value)"
        >
          <component :is="item.icon" class="nav-icon" />
          <span class="nav-text">
            <span class="nav-name">{{ item.label }}</span>
            <span class="nav-desc">{{ item.desc }}</span>
          </span>
        </button>

        <div class="nav-foot">
          <span class="nav-foot-title">已入库？</span>
          <a-button type="link" size="small" class="nav-link" @click="goDocs">文档列表</a-button>
        </div>
      </aside>

      <!-- ========== 右：工作区 ========== -->
      <section class="ingest-main">
        <header class="main-head">
          <div class="main-head-text">
            <h2 class="main-title">{{ currentMode.label }}</h2>
            <p class="main-hint">{{ currentMode.hint }}</p>
          </div>
          <a-tag v-if="mode === 'file' && files.length" color="blue">
            已选 {{ files.length }} 个 / {{ fmtSize(totalBytes) }}
          </a-tag>
        </header>

        <div class="main-body">
          <!-- ---------- 文件 ---------- -->
          <template v-if="mode === 'file'">
            <div
              class="drop-zone"
              :class="{ 'is-over': dragOver, 'is-filled': files.length > 0 }"
              @click="fileInput?.click()"
              @dragover.prevent="dragOver = true"
              @dragleave="onDragLeave"
              @drop.prevent="onDrop"
            >
              <CloudUploadOutlined class="drop-icon" />
              <div class="drop-title">把文件拖到这里，或点击选择</div>
              <div class="drop-sub">{{ supportedFormats }}</div>
            </div>
            <input
              ref="fileInput"
              type="file"
              multiple
              :accept="acceptAttr"
              hidden
              @change="onPickFile"
            />

            <div v-if="files.length" class="file-list">
              <div v-for="(f, i) in files" :key="`${f.name}-${i}`" class="file-row">
                <span class="file-ext">{{ extLabel(f.name) }}</span>
                <span class="file-name" :title="f.name">{{ f.name }}</span>
                <span class="file-size">{{ fmtSize(f.size) }}</span>
                <a-button
                  type="text"
                  size="small"
                  class="file-del"
                  :aria-label="`移除 ${f.name}`"
                  @click.stop="removeFile(i)"
                >
                  <CloseOutlined />
                </a-button>
              </div>
              <div class="file-foot">
                <span>共 {{ files.length }} 个文件 · 单个可自定义标题，多选则取文件名主干</span>
                <a-button type="link" size="small" class="nav-link" @click="clearFiles">清空</a-button>
              </div>
            </div>

            <div v-if="files.length === 1" class="field">
              <label class="field-label">文档标题（留空则用文件名）</label>
              <a-input v-model:value="form.title" placeholder="如：产品手册 v2" :maxlength="200" />
            </div>

            <p class="note">
              图片入库时由后端视觉模型生成文字描述供检索 / 问答（单张 ≤ 5MB，
              需后端配置支持视觉的对话模型）
            </p>
          </template>

          <!-- ---------- 文本 ---------- -->
          <template v-else-if="mode === 'text'">
            <div class="field">
              <label class="field-label is-required">文档标题<span class="req-tip">同名会覆盖旧版本</span></label>
              <a-input v-model:value="form.title" placeholder="如：产品手册 v2" :maxlength="200" />
            </div>
            <div class="field">
              <label class="field-label">正文格式</label>
              <a-radio-group
                v-model:value="form.format"
                :options="formatOptions"
                option-type="button"
                button-style="solid"
                size="small"
              />
              <span class="field-inline-hint">{{ formatHint }}</span>
            </div>
            <div class="field">
              <label class="field-label is-required">正文</label>
              <MarkdownEditor
                v-model="form.content"
                :format="form.format"
                :rows="16"
                placeholder="粘贴要入库的内容（选 Markdown 时可直接写标题 / 列表 / 表格，⌘V 粘截图会直接插图）"
              />
            </div>
          </template>

          <!-- ---------- 问答对 ---------- -->
          <template v-else-if="mode === 'pairs'">
            <div class="field">
              <label class="field-label is-required">文档标题<span class="req-tip">同名会覆盖旧版本</span></label>
              <a-input v-model:value="form.title" placeholder="如：售后常见问题" :maxlength="200" />
            </div>
            <div class="field">
              <label class="field-label">批量粘贴（每行一条：问题 | 答案，支持从 Excel 粘贴）</label>
              <a-textarea
                v-model:value="pairPaste"
                :rows="3"
                placeholder="如何退款？ | 签收后 7 个工作日内可申请全额退款。"
              />
              <a-button size="small" class="paste-btn" @click="onApplyPaste">解析并追加</a-button>
            </div>
            <div class="field">
              <label class="field-label">
                问答对
                <span class="label-meta">共 {{ pairs.length }} 条 · 有效 {{ validPairs.length }} 条</span>
              </label>
              <div class="pair-list">
                <div v-for="(pair, index) in pairs" :key="index" class="pair-row">
                  <span class="pair-index">{{ index + 1 }}</span>
                  <a-input v-model:value="pair.question" placeholder="问题" :maxlength="2000" />
                  <a-textarea v-model:value="pair.answer" :rows="2" placeholder="答案" />
                  <a-button type="text" size="small" class="file-del" @click="removePairRow(index)">
                    <CloseOutlined />
                  </a-button>
                </div>
                <a-button size="small" class="paste-btn" @click="addPairRow">＋ 添加一条</a-button>
                <p v-if="droppedPairs" class="note is-warn">
                  {{ droppedPairs }} 条问题或答案为空，入库时会被跳过
                </p>
              </div>
            </div>
          </template>

          <!-- ---------- URL ---------- -->
          <template v-else>
            <div class="field">
              <label class="field-label is-required">网页地址</label>
              <a-input
                v-model:value="form.url"
                placeholder="https://example.com/docs/deploy"
                :maxlength="2000"
              >
                <template #prefix><GlobalOutlined class="input-icon" /></template>
              </a-input>
              <p class="note">抓取后会剔除导航 / 脚本只留正文；正文过长会在尾部截断</p>
            </div>
            <div class="field">
              <label class="field-label">标题覆盖（可选）</label>
              <a-input v-model:value="form.urlTitle" placeholder="留空则用页面自带标题" :maxlength="200" />
            </div>
          </template>
        </div>

        <!-- 底部固定操作条：所有方式共用分片长度与提交 -->
        <footer class="main-foot">
          <div class="foot-field">
            <span class="foot-label">分片长度</span>
            <a-input-number
              v-model:value="form.chunkSize"
              :min="100"
              :max="4000"
              :step="100"
              size="small"
              class="foot-input"
            />
            <a-tooltip title="每个分片的目标字符数；太小会切断语义，太大则检索不精准">
              <span class="foot-help">?</span>
            </a-tooltip>
          </div>
          <a-space :size="8">
            <a-button @click="resetAll">重置</a-button>
            <a-button type="primary" :loading="submitting" @click="onSubmit">开始入库</a-button>
          </a-space>
        </footer>
      </section>
    </div>

    <!-- ========== 批量结果 ========== -->
    <div v-if="batchResults.length" class="result-panel">
      <div class="result-head">
        <span class="result-title">批量入库结果</span>
        <span class="result-sum">
          成功 {{ batchResults.length - batchErrors.length }} / {{ batchResults.length }}
        </span>
        <a-button type="link" size="small" class="nav-link" @click="batchResults = []">收起</a-button>
      </div>
      <div
        v-for="item in batchResults"
        :key="item.filename"
        class="result-row"
        :class="{ 'is-error': item.status === 'error' }"
      >
        <a-tag :color="item.status === 'success' ? 'green' : 'red'" class="result-tag">
          {{ item.status === 'success' ? '成功' : '失败' }}
        </a-tag>
        <span class="result-name">{{ item.filename }}</span>
        <span class="result-meta">
          {{
            item.status === 'success'
              ? `${item.chunks} 分片 / ${item.chars} 字`
              : item.error
          }}
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ingest-page {
  --accent: #2563eb;
  --radius: 10px;
}
.warn-alert { margin-bottom: 16px; }

/* ---------------- 骨架：左导航 + 右工作区 ---------------- */
.ingest-shell {
  display: flex;
  align-items: flex-start;
  gap: 16px;
}
.ingest-nav {
  flex: 0 0 228px;
  position: sticky;
  top: 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px;
  border: 1px solid var(--app-border);
  border-radius: var(--radius);
  background: var(--app-panel);
}
.nav-caption {
  font-size: 12px;
  color: var(--app-muted);
  letter-spacing: 0.4px;
  padding: 0 4px 6px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 10px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: none;
  color: var(--app-fg);
  text-align: left;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.nav-item:hover { background: var(--app-hover); }
.nav-item.is-active {
  background: var(--app-hover);
  /* 品牌色描边（刻意用固定色而非变量，与菜单选中色 #2563eb 对齐）*/
  border-color: rgba(37, 99, 235, 0.35);
}
.nav-icon { font-size: 16px; color: var(--app-muted); flex: 0 0 auto; }
.nav-item.is-active .nav-icon { color: var(--accent); }
.nav-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.nav-name { font-size: 13px; font-weight: 600; line-height: 1.3; }
.nav-desc { font-size: 12px; color: var(--app-muted); line-height: 1.3; }
.nav-item.is-active .nav-name { color: var(--accent); }
.nav-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  margin-top: 8px;
  padding-top: 10px;
  border-top: 1px dashed var(--app-border);
}
.nav-foot-title { font-size: 12px; color: var(--app-muted); }
.nav-link { padding: 0; height: auto; font-size: 12px; }

/* ---------------- 工作区 ---------------- */
.ingest-main {
  flex: 1 1 auto;
  min-width: 0;
  border: 1px solid var(--app-border);
  border-radius: var(--radius);
  background: var(--app-panel);
}
.main-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  /* 窄屏：右侧「已选 N 个」标签换到下一行，不把标题挤成一列字 */
  flex-wrap: wrap;
  gap: 12px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--app-border);
  background: var(--app-panel-2);
  border-radius: var(--radius) var(--radius) 0 0;
}
.main-title { margin: 0; font-size: 15px; font-weight: 600; color: var(--app-fg); }
.main-hint { margin: 4px 0 0; font-size: 12px; line-height: 1.6; color: var(--app-muted); }
.main-body { padding: 16px 18px 4px; }

/* ---------------- 字段 ---------------- */
.field { margin-bottom: 16px; }
.field-label {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
  font-weight: 500;
  color: var(--app-fg);
  margin-bottom: 6px;
}
.field-label.is-required::before { content: '*'; color: #dc2626; margin-right: 2px; }
.req-tip,
.label-meta,
.field-inline-hint {
  font-size: 12px;
  font-weight: 400;
  color: var(--app-muted);
}
.field-inline-hint { margin-left: 10px; }
.input-icon { color: var(--app-muted); }
.note { margin: 6px 0 0; font-size: 12px; line-height: 1.6; color: var(--app-muted); }
.note.is-warn { color: #d46b08; }

/* ---------------- 拖拽区 + 文件列表 ---------------- */
.drop-zone {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 26px 18px;
  border: 1px dashed var(--app-border);
  border-radius: var(--radius);
  background: var(--app-panel-2);
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}
.drop-zone:hover,
.drop-zone.is-over {
  border-color: var(--accent);
  background: var(--app-hover);
}
.drop-zone.is-filled { padding: 16px 18px; }
.drop-icon { font-size: 24px; color: var(--accent); }
.drop-title { font-size: 13px; font-weight: 600; color: var(--app-fg); }
.drop-sub { font-size: 12px; color: var(--app-muted); text-align: center; line-height: 1.6; }

.file-list {
  margin-top: 10px;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  overflow: hidden;
}
.file-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 10px;
  border-bottom: 1px solid var(--app-border);
  font-size: 13px;
}
.file-row:last-of-type { border-bottom: none; }
.file-row:hover { background: var(--app-hover); }
.file-ext {
  flex: 0 0 auto;
  min-width: 38px;
  text-align: center;
  font-size: 11px;
  letter-spacing: 0.3px;
  color: var(--accent);
  background: var(--app-panel-2);
  border: 1px solid var(--app-border);
  border-radius: 4px;
  padding: 1px 4px;
}
.file-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--app-fg);
}
.file-size { flex: 0 0 auto; font-size: 12px; color: var(--app-muted); }
.file-del { flex: 0 0 auto; color: var(--app-muted); }
.file-del:hover { color: #dc2626; }
.file-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 5px 10px;
  background: var(--app-panel-2);
  border-top: 1px solid var(--app-border);
  font-size: 12px;
  color: var(--app-muted);
}

/* ---------------- 问答对 ---------------- */
.paste-btn { margin-top: 6px; }
.pair-list {
  max-height: 340px;
  overflow: auto;
  padding: 8px;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-panel-2);
}
.pair-row {
  display: grid;
  grid-template-columns: 22px 1fr 1.6fr 28px;
  gap: 8px;
  align-items: start;
  margin-bottom: 8px;
}
.pair-index {
  font-size: 12px;
  color: var(--app-muted);
  padding-top: 7px;
  text-align: right;
}

/* ---------------- 底部操作条 ---------------- */
.main-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  /* 窄屏：分片长度与「重置 / 开始入库」不同一排时换行，不整条撑出工作区 */
  flex-wrap: wrap;
  gap: 12px;
  padding: 10px 18px;
  border-top: 1px solid var(--app-border);
  background: var(--app-panel-2);
  border-radius: 0 0 var(--radius) var(--radius);
}
.foot-field { display: flex; align-items: center; gap: 8px; }
.foot-label { font-size: 13px; color: var(--app-fg); }
.foot-input { width: 110px; }
.foot-help {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 1px solid var(--app-border);
  background: var(--app-panel);
  font-size: 11px;
  color: var(--app-muted);
  cursor: help;
}

/* ---------------- 批量结果 ---------------- */
.result-panel {
  margin-top: 16px;
  border: 1px solid var(--app-border);
  border-radius: var(--radius);
  background: var(--app-panel);
  overflow: hidden;
}
.result-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--app-border);
  background: var(--app-panel-2);
}
.result-title { font-size: 13px; font-weight: 600; color: var(--app-fg); }
.result-sum { font-size: 12px; color: var(--app-muted); margin-left: auto; }
.result-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 6px 14px;
  font-size: 13px;
  border-bottom: 1px solid var(--app-border);
}
.result-row:last-child { border-bottom: none; }
.result-row.is-error { background: rgba(220, 38, 38, 0.04); }
.result-tag { flex: 0 0 auto; margin: 0; }
.result-name { flex: 0 0 auto; max-width: 45%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.result-meta { flex: 1 1 auto; font-size: 12px; color: var(--app-muted); word-break: break-all; }

/* ---------------- 窄屏：导航横过来，工作区在下 ---------------- */
/* 侧栏已经占掉 240px，媒体查询按视口宽算会把真实可用宽算大；
   笔记本宽度（≤1280）工作区只剩不到 750px，直接改为上下堆叠更舒展。 */
@media (max-width: 1280px) {
  .ingest-shell { flex-direction: column; }
  .ingest-nav {
    position: static;
    flex: none;
    width: 100%;
    flex-direction: row;
    overflow-x: auto;
    gap: 8px;
  }
  .nav-caption,
  .nav-foot { display: none; }
  .nav-item { width: auto; flex: 0 0 auto; }
  .nav-desc { display: none; }
}
</style>
