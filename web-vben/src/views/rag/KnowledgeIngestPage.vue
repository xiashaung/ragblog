<script setup lang="ts">
/**
 * 知识库 · 内容录入 —— 四种入库方式（文件多选 / 文本 / 问答对 / URL）从弹窗升级为独立页面。
 * 向量化模型由后端 .env 配置决定，前端不选择平台；入库后可跳转「文档列表」查看与检索。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import { Card, message } from 'ant-design-vue';
import { ragApi, uploadApi } from '@/api';
import { useBusinessStore } from '@/store/modules/business';
import type { KnowledgePair, UploadBatchItem } from '@/typings';

const router = useRouter();
const business = useBusinessStore();

type AddMode = 'file' | 'text' | 'pairs' | 'url';

const allowedExts = ['pdf', 'docx', 'txt', 'md', 'csv', 'log', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'];
const supportedFormats = '支持 PDF / DOCX / TXT / MD / CSV / LOG，以及图片 PNG / JPG / GIF / WEBP / BMP';
const MAX_BATCH_FILES = 20;

const mode = ref<AddMode>('file');
const form = reactive({
  title: '',
  content: '',
  chunkSize: 500,
  url: '',
  urlTitle: '',
  maxChars: 200_000,
});
const files = ref<File[]>([]);
const pairs = ref<KnowledgePair[]>([{ question: '', answer: '' }]);
const pairPaste = ref('');
const submitting = ref(false);
// 批量上传部分失败时留在页面逐条展示，不直接清掉
const batchResults = ref<UploadBatchItem[]>([]);

const modeOptions = [
  { label: '上传文件', value: 'file' },
  { label: '文本录入', value: 'text' },
  { label: '问答对', value: 'pairs' },
  { label: 'URL 抓取', value: 'url' },
];

const validPairs = computed(() =>
  pairs.value
    .map((p) => ({ question: p.question.trim(), answer: p.answer.trim() }))
    .filter((p) => p.question && p.answer),
);
const droppedPairs = computed(() => pairs.value.length - validPairs.value.length);
const batchErrors = computed(() => batchResults.value.filter((r) => r.status === 'error'));

// 切换录入方式时清掉与上一方式相关的临时输入，避免串味
function onModeChange() {
  form.title = '';
  form.content = '';
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

function onPickFile(ev: Event) {
  const picked = Array.from((ev.target as HTMLInputElement).files || []);
  const good = picked.filter((f) => allowedExts.includes(fileExt(f.name)));
  if (good.length !== picked.length) {
    message.warning(`已忽略 ${picked.length - good.length} 个不支持的文件，${supportedFormats}`);
  }
  let fs = good;
  if (fs.length > MAX_BATCH_FILES) {
    message.warning(`一次最多 ${MAX_BATCH_FILES} 个，多余的已忽略`);
    fs = fs.slice(0, MAX_BATCH_FILES);
  }
  files.value = fs;
  batchResults.value = [];
  // 单文件时允许自定义标题；多选时标题只能取文件名（一个请求对应多个文档）
  if (fs.length === 1 && !form.title) {
    form.title = fs[0].name.replace(/\.[^.]+$/, '');
  }
}

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

function summarizeBatch(results: UploadBatchItem[]) {
  batchResults.value = results;
  const ok = results.filter((r) => r.status === 'success');
  const bad = results.filter((r) => r.status === 'error');
  if (ok.length) message.success(`已入库 ${ok.length} 个文件`);
  if (bad.length) message.warning(`${bad.length} 个文件未入库，详见下方列表`);
}

function onTextInput(ev: Event) {
  form.content = (ev.target as HTMLTextAreaElement).value;
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
  <div class="page-card">
    <a-alert v-if="!business.backendOk" type="warning" show-icon style="margin-bottom: 12px"
      message="后端当前未响应（/health 未通过），入库可能失败" />

    <Card :bordered="false">
      <template #title>录入知识</template>
      <template #extra>
        <a-button type="link" @click="goDocs">查看文档列表</a-button>
      </template>

      <a-radio-group
        v-model:value="mode"
        :options="modeOptions"
        option-type="button"
        button-style="solid"
        style="margin-bottom: 16px"
        @change="onModeChange"
      />

      <a-form layout="vertical" style="max-width: 720px">
        <template v-if="mode === 'file'">
          <a-form-item label="文件（可多选，一次最多 20 个）">
            <input type="file" multiple :accept="allowedExts.map((e) => `.${e}`).join(',')" @change="onPickFile" />
            <div class="sub">{{ supportedFormats }}；多选时逐文件入库，标题取文件名主干（同名覆盖）</div>
            <div class="sub">图片入库时由后端视觉模型生成文字描述供检索/问答（单张≤ 5MB，需后端配置支持视觉的对话模型）</div>
            <div v-if="files.length" class="sub" style="margin-top: 6px">
              已选 {{ files.length }} 个：{{ files.map((f) => f.name).join('、') }}
            </div>
          </a-form-item>
          <a-form-item v-if="files.length === 1" label="文档标题（留空则用文件名）">
            <a-input v-model:value="form.title" placeholder="如：产品手册 v2" :maxlength="200" />
          </a-form-item>
        </template>

        <template v-else>
          <a-form-item label="文档标题（同名会覆盖旧版本）" required>
            <a-input v-model:value="form.title" :placeholder="mode === 'url' ? '留空则用页面标题' : '如：产品手册 v2'" :maxlength="200" />
          </a-form-item>
        </template>

        <template v-if="mode === 'text'">
          <a-form-item label="正文" required>
            <a-textarea
              :value="form.content"
              :rows="12"
              placeholder="粘贴要入库的文档内容"
              @input="onTextInput"
            />
          </a-form-item>
        </template>

        <template v-else-if="mode === 'pairs'">
          <a-form-item label="批量粘贴问答对（每行一条：问题 | 答案，支持从 Excel 粘贴）">
            <a-textarea
              v-model:value="pairPaste"
              :rows="4"
              placeholder="如何退款？ | 签收后 7 个工作日内可申请全额退款。"
            />
            <a-button size="small" style="margin-top: 6px" @click="onApplyPaste">解析并追加</a-button>
          </a-form-item>
          <a-form-item :label="`问答对（共 ${pairs.length} 条，有效 ${validPairs.length} 条）`">
            <div class="pair-list">
              <div v-for="(pair, index) in pairs" :key="index" class="pair-row">
                <span class="pair-index">{{ index + 1 }}</span>
                <a-input v-model:value="pair.question" placeholder="问题" :maxlength="2000" />
                <a-textarea v-model:value="pair.answer" :rows="2" placeholder="答案" />
                <a-button size="small" danger @click="removePairRow(index)">删除</a-button>
              </div>
              <a-button size="small" style="margin-top: 6px" @click="addPairRow">＋ 添加一条</a-button>
              <div v-if="droppedPairs" class="sub" style="margin-top: 4px">
                {{ droppedPairs }} 条问题或答案为空，入库时会被跳过
              </div>
            </div>
          </a-form-item>
        </template>

        <template v-else-if="mode === 'url'">
          <a-form-item label="网页地址" required>
            <a-input v-model:value="form.url" placeholder="https://example.com/docs/deploy" :maxlength="2000" />
            <div class="sub">后端抓取并剔除导航/脚本只留正文；动态渲染或需登录的页面抓不到内容</div>
          </a-form-item>
          <a-form-item label="标题覆盖（可选）">
            <a-input v-model:value="form.urlTitle" placeholder="留空则用页面自带标题" :maxlength="200" />
          </a-form-item>
        </template>

        <a-form-item label="分片长度（字符）">
          <a-input-number v-model:value="form.chunkSize" :min="100" :max="4000" :step="100" />
          <span class="sub" style="margin-left: 8px">问答对建议 800 以上，尽量让一条问答对落在同一分片里</span>
        </a-form-item>

        <a-form-item>
          <a-space>
            <a-button type="primary" :loading="submitting" @click="onSubmit">开始入库</a-button>
            <a-button @click="resetAll">重置</a-button>
          </a-space>
        </a-form-item>
      </a-form>

      <template v-if="batchResults.length">
        <a-divider style="margin: 8px 0" />
        <div class="sub" style="margin-bottom: 6px">
          批量结果：成功 {{ batchResults.length - batchErrors.length }} / {{ batchResults.length }}
        </div>
        <div class="batch-list">
          <div v-for="item in batchResults" :key="item.filename" class="batch-row">
            <a-tag :color="item.status === 'success' ? 'green' : 'red'">
              {{ item.status === 'success' ? '成功' : '失败' }}
            </a-tag>
            <span>{{ item.filename }}</span>
            <span class="sub">
              {{ item.status === 'success' ? `${item.chunks} 分片 / ${item.chars} 字` : item.error }}
            </span>
          </div>
        </div>
      </template>
    </Card>
  </div>
</template>

<style scoped>
.sub {
  font-size: 12px;
  color: var(--app-muted);
  margin-top: 2px;
}
.pair-list {
  max-height: 320px;
  overflow: auto;
}
.pair-row {
  display: grid;
  grid-template-columns: 28px 1fr 2fr 56px;
  gap: 8px;
  align-items: start;
  margin-bottom: 8px;
}
.pair-index {
  font-size: 12px;
  color: var(--app-muted);
  padding-top: 6px;
  text-align: right;
}
.batch-list {
  max-height: 200px;
  overflow: auto;
  border: 1px solid var(--app-border);
  border-radius: 6px;
  padding: 6px 8px;
}
.batch-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
  padding: 2px 0;
}
.batch-row .sub {
  margin-top: 0;
  word-break: break-all;
}
</style>
