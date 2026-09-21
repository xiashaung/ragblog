<script setup lang="ts">
/**
 * Markdown 编辑器 —— 文本域 + 工具栏 + 三态视图（编辑 / 分栏 / 预览），零依赖。
 *
 * 为什么不做所见即所得：库里只存 Markdown 源码（分块、向量、检索都因此干净），
 * WYSIWYG 要么拖进上百万的编辑器依赖，要么把源码改坏回不去。工具栏只是给选区
 * 包一层语法、给行首加个前缀，是「源码编辑」的加速器，不是替代品。
 *
 * 图片走 ``/upload/image`` 落盘后插 ``![](/media/uploads/x.png)``：正文里只留地址，
 * base64 不进分块与向量。工具栏与插图只在 ``format="markdown"`` 时开放：
 * 纯文本不解析语法、HTML 的排版靠粘贴带进来，两者套 Markdown 标记只会写成乱码；
 * 转换本身由后端完成（见 app/services/markdown_text.py）。
 */
import { computed, nextTick, ref } from 'vue';
import { message } from 'ant-design-vue';
import { uploadApi } from '@/api';
import RichText from '@/components/RichText.vue';
import type { ContentFormat } from '@/typings';

const props = withDefaults(
  defineProps<{
    modelValue: string;
    /** 正文格式：决定工具栏是否适用，以及粘贴时保留哪种剪贴板内容 */
    format?: ContentFormat;
    placeholder?: string;
    /** 文本域初始行数（分栏时另一半同样高度） */
    rows?: number;
    defaultView?: 'edit' | 'split' | 'preview';
  }>(),
  { format: 'markdown', placeholder: '', rows: 14, defaultView: 'split' },
);

const emit = defineEmits<{ 'update:modelValue': [value: string] }>();

const ta = ref<HTMLTextAreaElement | null>(null);
const fileEl = ref<HTMLInputElement | null>(null);
const uploading = ref(false);
const view = ref<'edit' | 'split' | 'preview'>(props.defaultView);

const VIEW_OPTIONS = [
  { label: '编辑', value: 'edit' },
  { label: '分栏', value: 'split' },
  { label: '预览', value: 'preview' },
];

/** html 模式的正文是给后端转换的源码，纯文本模式则不解析语法：两者都不适用 Markdown 工具栏 */
const isHtml = computed(() => props.format === 'html');
const canStyle = computed(() => props.format === 'markdown');
const showEditor = computed(() => view.value !== 'preview');
const showPreview = computed(() => view.value !== 'edit');
const charCount = computed(() => props.modelValue.length);
const imageCount = computed(() => (props.modelValue.match(/!\[[^\]]*\]\([^)]*\)/g) || []).length);
/** 分栏时两栏等高（按行数算，不然左边文本域、右边预览底边锯齿得难看）*/
const paneHeight = computed(() => `${props.rows * 26}px`);

/** 统一的一次改动：写回父组件并把光标/选区放回该放的地方（受控 textarea 要等 DOM 更新） */
function apply(next: string, selStart: number, selEnd = selStart) {
  emit('update:modelValue', next);
  nextTick(() => {
    const el = ta.value;
    if (!el) return;
    el.focus();
    el.setSelectionRange(selStart, selEnd);
  });
}

function currentRange() {
  const el = ta.value;
  if (!el) return { value: '', start: 0, end: 0 };
  return { value: el.value, start: el.selectionStart, end: el.selectionEnd };
}

/** 包裹选区；没选内容时插入一对标记 + 占位文字，光标停在占位文字上 */
function wrap(before: string, after = before, placeholder = '文字') {
  const { value, start, end } = currentRange();
  const picked = value.slice(start, end) || placeholder;
  apply(value.slice(0, start) + before + picked + after + value.slice(end), start + before.length, start + before.length + picked.length);
}

/** 给选中的每一行加前缀（``toggle`` 时已有前缀的行改为取消，方便反悔） */
function prefixLines(prefix: string, toggle = false) {
  const { value, start, end } = currentRange();
  const from = value.lastIndexOf('\n', start - 1) + 1; // 扩到整行：前缀只有落在行首才有效
  const to = value.indexOf('\n', end) === -1 ? value.length : value.indexOf('\n', end);
  const block = value
    .slice(from, to)
    .split('\n')
    .map((line) => {
      if (toggle && line.startsWith(prefix)) return line.slice(prefix.length);
      return `${prefix}${line.replace(/^(#{1,6}|>|[-*+]|\d+[.)])\s+/, '')}`;
    })
    .join('\n');
  apply(value.slice(0, from) + block + value.slice(to), from, from + block.length);
}

/** 围栏代码块：整块重起一行，避免粘在句子后面变成行内代码 */
function insertCodeBlock() {
  const { value, start, end } = currentRange();
  const picked = value.slice(start, end);
  const body = picked || '代码';
  const pad = start > 0 && value[start - 1] !== '\n' ? '\n' : '';
  const text = `${pad}\`\`\`\n${body}\n\`\`\`\n`;
  apply(value.slice(0, start) + text + value.slice(end), start + pad.length + 4, start + pad.length + 4 + body.length);
}

function insertBlock(text: string) {
  const { value, start, end } = currentRange();
  const pad = start > 0 && value[start - 1] !== '\n' ? '\n\n' : value.slice(0, start).endsWith('\n\n') ? '' : '\n';
  const body = `${pad}${text}\n`;
  apply(value.slice(0, start) + body + value.slice(end), start + body.length);
}

/** 在光标处插一段文字（Tab 缩进用，不碰选区内容） */
function insertText(text: string) {
  const { value, start, end } = currentRange();
  apply(value.slice(0, start) + text + value.slice(end), start + text.length);
}

const TABLE_SAMPLE = '| 列一 | 列二 |\n| --- | --- |\n| 内容 | 内容 |';

function onKeydown(ev: KeyboardEvent) {
  if (!(ev.metaKey || ev.ctrlKey)) {
    // Tab 缩进：编辑器场景下没人希望焦点跳走
    if (ev.key === 'Tab' && canStyle.value) {
      ev.preventDefault();
      insertText('  ');
    }
    return;
  }
  if (!canStyle.value) return;
  const key = ev.key.toLowerCase();
  const map: Record<string, () => void> = {
    b: () => wrap('**', '**', '粗体'),
    i: () => wrap('*', '*', '斜体'),
    k: () => wrap('[', '](https://)', '链接文字'),
    e: () => wrap('`', '`', 'code'),
  };
  const hit = map[key];
  if (!hit) return;
  ev.preventDefault();
  hit();
}

/**
 * 粘贴：Markdown 模式下拦截截图/图片文件，直接上传后插地址；
 * html 模式额外抢一次粘剪贴板里的 ``text/html``——浏览器默认只会粘纯文本，
 * 排版信息就在这一步丢掉。
 */
function onPaste(ev: ClipboardEvent) {
  if (canStyle.value) {
    const images = Array.from(ev.clipboardData?.files || []).filter((f) =>
      f.type.startsWith('image/'),
    );
    if (images.length) {
      ev.preventDefault();
      void uploadFiles(images);
      return;
    }
  }
  if (!isHtml.value) return;
  const html = ev.clipboardData?.getData('text/html') || '';
  if (!html.trim()) return;
  ev.preventDefault();
  insertText(html.trim());
}

function insertImage(name: string, url: string) {
  insertBlock(`![${name.replace(/\.[^.]+$/, '')}](${url})`);
}

async function uploadFiles(files: File[]) {
  const images = files.filter((f) => f.type.startsWith('image/'));
  if (!images.length) return message.warning('请选择图片文件');
  uploading.value = true;
  let done = 0;
  for (const file of images) {
    try {
      const res = await uploadApi.uploadImage(file);
      insertImage(res.name || file.name, res.url);
      done += 1;
    } catch (e: any) {
      message.error(`${file.name}：${e?.detail || e?.message || '上传失败'}`);
    }
  }
  uploading.value = false;
  if (done) message.success(`已插入 ${done} 张图片`);
}

function onPickImages(ev: Event) {
  const input = ev.target as HTMLInputElement;
  const picked = Array.from(input.files || []);
  input.value = ''; // 同一张图再选一次也要能触发 change
  void uploadFiles(picked);
}
</script>

<template>
  <div class="md-editor">
    <div class="md-toolbar">
      <a-button-group size="small">
        <a-tooltip title="一级标题（行首 #）">
          <a-button type="text" :disabled="!canStyle" @click="prefixLines('# ', true)">H1</a-button>
        </a-tooltip>
        <a-tooltip title="二级标题（行首 ##）">
          <a-button type="text" :disabled="!canStyle" @click="prefixLines('## ', true)">H2</a-button>
        </a-tooltip>
        <a-tooltip title="三级标题（行首 ###）">
          <a-button type="text" :disabled="!canStyle" @click="prefixLines('### ', true)">H3</a-button>
        </a-tooltip>
      </a-button-group>
      <a-button-group size="small">
        <a-tooltip title="加粗 ⌘B">
          <a-button type="text" :disabled="!canStyle" @click="wrap('**', '**', '粗体')">粗体</a-button>
        </a-tooltip>
        <a-tooltip title="斜体 ⌘I">
          <a-button type="text" :disabled="!canStyle" @click="wrap('*', '*', '斜体')">斜体</a-button>
        </a-tooltip>
        <a-tooltip title="删除线">
          <a-button type="text" :disabled="!canStyle" @click="wrap('~~', '~~', '作废')">删除</a-button>
        </a-tooltip>
        <a-tooltip title="行内代码 ⌘E">
          <a-button type="text" :disabled="!canStyle" @click="wrap('`', '`', 'code')">代码</a-button>
        </a-tooltip>
      </a-button-group>
      <a-button-group size="small">
        <a-tooltip title="无序列表">
          <a-button type="text" :disabled="!canStyle" @click="prefixLines('- ', true)">• 列表</a-button>
        </a-tooltip>
        <a-tooltip title="有序列表">
          <a-button type="text" :disabled="!canStyle" @click="prefixLines('1. ', true)">1. 列表</a-button>
        </a-tooltip>
        <a-tooltip title="引用">
          <a-button type="text" :disabled="!canStyle" @click="prefixLines('> ', true)">引用</a-button>
        </a-tooltip>
        <a-tooltip title="围栏代码块">
          <a-button type="text" :disabled="!canStyle" @click="insertCodeBlock">代码块</a-button>
        </a-tooltip>
      </a-button-group>
      <a-button-group size="small">
        <a-tooltip title="链接 ⌘K">
          <a-button type="text" :disabled="!canStyle" @click="wrap('[', '](https://)', '链接文字')">链接</a-button>
        </a-tooltip>
        <a-tooltip :title="canStyle ? '插入图片（支持多选）' : '仅 Markdown 模式适用插图'">
          <a-button type="text" :disabled="!canStyle || uploading" :loading="uploading" @click="fileEl?.click()">
            图片
          </a-button>
        </a-tooltip>
        <a-tooltip title="表格模板">
          <a-button type="text" :disabled="!canStyle" @click="insertBlock(TABLE_SAMPLE)">表格</a-button>
        </a-tooltip>
        <a-tooltip title="分割线">
          <a-button type="text" :disabled="!canStyle" @click="insertBlock('---')">分割线</a-button>
        </a-tooltip>
      </a-button-group>
      <div class="md-tools-right">
        <a-radio-group v-model:value="view" :options="VIEW_OPTIONS" size="small" option-type="button" button-style="solid" />
      </div>
    </div>

    <div class="md-body" :class="{ 'is-split': showEditor && showPreview }">
      <textarea
        v-if="showEditor"
        ref="ta"
        class="md-source"
        :value="modelValue"
        :rows="rows"
        :placeholder="placeholder"
        spellcheck="false"
        @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
        @keydown="onKeydown"
        @paste="onPaste"
      />
      <div v-if="showPreview" class="md-preview">
        <div v-if="isHtml" class="md-preview-html">
          <a-alert
            type="info"
            show-icon
            message="HTML 源码预览"
            description="保存入库时后端会把这段 HTML 转成 Markdown（标题、列表、表格、链接、图片都保留），之后在文档详情里按 Markdown 渲染。"
          />
          <pre class="md-raw">{{ modelValue || '（还没粘贴内容）' }}</pre>
        </div>
        <!-- 纯文本不解析语法，预览就按原样显示，免得把 # | 误读成排版 -->
        <pre v-else-if="!canStyle" class="md-plain">{{ modelValue || '（还没输入内容）' }}</pre>
        <RichText v-else :text="modelValue" />
      </div>
    </div>

    <div class="md-foot">
      <span>{{ charCount }} 字</span>
      <span v-if="imageCount">· {{ imageCount }} 张图片</span>
      <span class="md-tip">
        {{
          isHtml
            ? '从网页 / Word 复制后直接粘贴，会带进 HTML 源码'
            : canStyle
              ? '支持 ⌘B / ⌘I / ⌘K / ⌘E，粘截图会直接上传插图'
              : '纯文本不解析 Markdown 语法，需要排版请切到 Markdown'
        }}
      </span>
    </div>

    <input ref="fileEl" type="file" accept="image/*" multiple hidden @change="onPickImages" />
  </div>
</template>

<style scoped>
.md-editor {
  --radius: 10px;
  border: 1px solid var(--app-border);
  border-radius: var(--radius);
  background: var(--app-panel);
  overflow: hidden;
}
.md-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border-bottom: 1px solid var(--app-border);
  background: var(--app-panel-2);
}
.md-tools-right { margin-left: auto; }
.md-body { display: flex; }
/* 分栏：两栏等宽等高，各自滚动，避免预览比源码长时把整块撑长 */
.md-body.is-split { height: v-bind(paneHeight); }
.md-body.is-split .md-source,
.md-body.is-split .md-preview { width: 50%; height: 100%; }
.md-body.is-split .md-source {
  resize: none;
  border-right: 1px solid var(--app-border);
  min-width: 0;
}
.md-source {
  width: 100%;
  border: 0;
  outline: none;
  resize: vertical;
  padding: 10px 12px;
  font-family: 'SF Mono', Monaco, Consolas, monospace;
  font-size: 13px;
  line-height: 1.7;
  color: var(--app-fg);
  background: var(--app-panel);
}
.md-preview {
  padding: 10px 12px;
  overflow: auto;
  min-height: 120px;
  max-height: 520px;
  background: var(--app-panel);
}
.md-body.is-split .md-preview { max-height: none; }
.md-preview-html .md-raw,
.md-preview .md-plain {
  margin: 8px 0 0;
  padding: 8px 10px;
  max-height: 240px;
  overflow: auto;
  background: var(--app-panel-2);
  border: 1px solid var(--app-border);
  border-radius: 6px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
.md-preview .md-plain {
  margin-top: 0;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  word-break: normal;
}
.md-foot {
  display: flex;
  gap: 6px;
  align-items: center;
  padding: 5px 10px;
  border-top: 1px solid var(--app-border);
  background: var(--app-panel-2);
  font-size: 12px;
  color: var(--app-muted);
}
.md-foot .md-tip { margin-left: auto; }
</style>
