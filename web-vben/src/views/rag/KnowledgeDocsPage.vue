<script setup lang="ts">
/**
 * 知识库 · 文档列表 —— 概览统计 + 文档列表（搜索 / 过滤 / 分页 / 批量删除）、
 * 详情与分片查看、改名 / 改内容 / 重建索引 / 删除。
 * 内容录入（文件 / 文本 / 问答对 / URL）已拆到独立「内容录入」页。
 * 向量化模型由后端 .env 配置决定，前端不选择平台。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
// 模板里用的是 PascalCase <Card>，全局注册只提供 a-*，必须显式引入
import { Card, message } from 'ant-design-vue';
import { ragApi } from '@/api';
import { usePerm } from '@/composables/usePerm';
import { useBusinessStore } from '@/store/modules/business';
import type { KnowledgeDetail, KnowledgeDocItem, KnowledgeStats } from '@/typings';

const router = useRouter();
const route = useRoute();
const business = useBusinessStore();
// 按钮级权限：改名 / 重建 / 删除 / 保存正文都是写操作，统一要 knowledge:manage。
// 能进这一页说明至少有 knowledge:read（路由守卫已挡），所以只需再区分读写。
const { can, tip: permHint } = usePerm();
const canManage = computed(() => can('knowledge:manage'));

const stats = ref<KnowledgeStats | null>(null);
const list = ref<KnowledgeDocItem[]>([]);
const loading = ref(false);
const total = ref(0);
const page = reactive({ current: 1, pageSize: 10 });
const filters = reactive({
  keyword: '',
  provider: undefined as string | undefined,
  onlyStale: false,
});
const selectedKeys = ref<string[]>([]);
const reindexing = ref('');

const columns = [
  { title: '标题', dataIndex: 'title', key: 'title', ellipsis: true },
  { title: 'Provider', dataIndex: 'provider', key: 'provider', width: 150 },
  { title: '分片', dataIndex: 'chunks', key: 'chunks', width: 70 },
  { title: '字符', dataIndex: 'chars', key: 'chars', width: 90 },
  { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at', width: 165 },
  { title: '操作', key: 'actions', width: 250 },
];

// provider 过滤项来自后端实际入库情况，而非硬编码枚举
const providerOptions = computed(() =>
  (stats.value?.by_provider || []).map((p) => ({
    label: `${p.provider}（${p.docs} 篇）`,
    value: p.provider,
  })),
);

const rowSelection = computed(() => ({
  selectedRowKeys: selectedKeys.value,
  onChange: (keys: (string | number)[]) => (selectedKeys.value = keys.map(String)),
}));

// 不能删就不给勾选：留着只会让人勾完才发现「批量删除」是灰的
const tableSelection = computed(() => (canManage.value ? rowSelection.value : undefined));

const kindLabels: Record<string, string> = {
  file: '文件',
  text: '文本',
  faq: '问答对',
  url: '网页',
};
const kindColors: Record<string, string> = {
  file: 'blue',
  text: 'default',
  faq: 'purple',
  url: 'cyan',
};

async function loadStats() {
  try {
    stats.value = await ragApi.stats();
  } catch {
    stats.value = null;
  }
}

async function loadList() {
  loading.value = true;
  try {
    const data = await ragApi.list({
      keyword: filters.keyword.trim() || undefined,
      provider: filters.provider,
      only_stale: filters.onlyStale || undefined,
      offset: (page.current - 1) * page.pageSize,
      limit: page.pageSize,
    });
    list.value = data?.items || [];
    total.value = data?.total || 0;
    selectedKeys.value = [];
  } catch (e: any) {
    list.value = [];
    total.value = 0;
    message.error(e?.detail || e?.message || '加载知识库列表失败');
  } finally {
    loading.value = false;
  }
}

async function refresh() {
  await Promise.all([loadList(), loadStats()]);
}

function onSearch() {
  page.current = 1;
  loadList();
}

function onFilterChange() {
  page.current = 1;
  loadList();
}

// antd 表格分页 / 排序变化
function onTableChange(pag: any) {
  page.current = pag?.current || 1;
  page.pageSize = pag?.pageSize || page.pageSize;
  loadList();
}

function goIngest() {
  router.push('/knowledge/ingest');
}

function fmtChars(n: number): string {
  return (n || 0).toLocaleString();
}

function formatDate(ts: number): string {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

/* ---------------- 详情抽屉：原文 + 分片 ---------------- */
const detail = ref<KnowledgeDetail | null>(null);
const detailVisible = ref(false);
const detailLoading = ref(false);
const editContent = ref('');
const saving = ref(false);

async function openDetail(row: KnowledgeDocItem) {
  detailVisible.value = true;
  detailLoading.value = true;
  detail.value = null;
  try {
    const data = await ragApi.detail(row.knowledge_id, 100);
    detail.value = data;
    editContent.value = data?.content || '';
  } catch (e: any) {
    message.error(e?.detail || e?.message || '加载文档详情失败');
  } finally {
    detailLoading.value = false;
  }
}

function onContentInput(ev: Event) {
  editContent.value = (ev.target as HTMLTextAreaElement).value;
}

// 保存正文 = 重新分片 + 重新向量化（成本与一次入库相同）
async function onSaveContent() {
  const doc = detail.value?.doc;
  if (!doc) return;
  if (!editContent.value.trim()) return message.warning('正文不能为空');
  saving.value = true;
  try {
    const updated = await ragApi.update(doc.knowledge_id, { content: editContent.value });
    message.success(`已更新并重建索引，共 ${updated.chunks} 个分片`);
    await refresh();
    await openDetail(updated);
  } catch (e: any) {
    message.error(e?.detail || e?.message || '保存失败');
  } finally {
    saving.value = false;
  }
}

/* ---------------- 改名 / 重建 / 删除 ---------------- */
const renameModal = reactive({ visible: false, id: '', title: '' });

function openRename(row: KnowledgeDocItem) {
  renameModal.visible = true;
  renameModal.id = row.knowledge_id;
  renameModal.title = row.title;
}

async function onRename() {
  if (!renameModal.title.trim()) return message.warning('标题不能为空');
  try {
    await ragApi.update(renameModal.id, { title: renameModal.title.trim() });
    message.success('标题已更新');
    renameModal.visible = false;
    await refresh();
  } catch (e: any) {
    message.error(e?.detail || e?.message || '改名失败');
  }
}

async function onReindex(row: KnowledgeDocItem) {
  reindexing.value = row.knowledge_id;
  try {
    const res = await ragApi.reindex(row.knowledge_id);
    message.success(
      `${res.previous_provider && res.previous_provider !== res.provider
        ? `已从 ${res.previous_provider} 切换到`
        : '已用'} ${res.provider} 重建 ${res.chunks} 个分片`,
    );
    await refresh();
  } catch (e: any) {
    message.error(e?.detail || e?.message || '重建索引失败');
  } finally {
    reindexing.value = '';
  }
}

async function onRemove(row: KnowledgeDocItem) {
  try {
    await ragApi.remove(row.knowledge_id);
    message.success('已删除');
    await refresh();
  } catch (e: any) {
    message.error(e?.detail || e?.message || '删除失败');
  }
}

async function onBatchRemove() {
  const ids = selectedKeys.value;
  if (!ids.length) return message.warning('请先勾选要删除的文档');
  try {
    const res = await ragApi.batchRemove(ids);
    message.success(
      res.failed?.length ? `已删除 ${res.deleted} 篇，${res.failed.length} 篇未找到` : `已删除 ${res.deleted} 篇`,
    );
    await refresh();
  } catch (e: any) {
    message.error(e?.detail || e?.message || '批量删除失败');
  }
}

onMounted(async () => {
  business.loadProviders();
  business.pollHealth();
  await refresh();
  // 从检索问答页的引用标题跳转过来时（/knowledge?doc=<id>），自动打开该文档详情。
  const docId = route.query.doc;
  if (typeof docId === 'string' && docId) {
    openDetail({ knowledge_id: docId } as KnowledgeDocItem);
  }
});
</script>

<template>
  <div class="page-card">
    <a-alert v-if="!business.backendOk" type="warning" show-icon style="margin-bottom: 12px"
      message="后端当前未响应（/health 未通过），知识库操作可能失败" />

    <!-- 概览统计已拆到独立「知识库概览」页，本页只保留文档表格 -->
    <Card :bordered="false">
      <template #title>知识库文档</template>
      <template #extra>
        <a-button v-perm="'knowledge:manage'" type="primary" @click="goIngest">录入内容</a-button>
      </template>

      <a-space wrap style="margin-bottom: 12px">
        <a-input-search
          v-model:value="filters.keyword"
          placeholder="按标题 / 正文搜索"
          style="width: 240px"
          allow-clear
          @search="onSearch"
          @change="(e: any) => { if (!e.target.value) onSearch(); }"
        />
        <a-select
          v-model:value="filters.provider"
          :options="providerOptions"
          placeholder="按 provider 过滤"
          style="width: 190px"
          allow-clear
          @change="onFilterChange"
        />
        <a-tooltip title="只列出向量 provider 与当前默认配置不一致的文档（检索不到）">
          <span>
            <a-switch
              :checked="filters.onlyStale"
              checked-children="仅待重建"
              un-children="全部"
              @change="(v: boolean) => { filters.onlyStale = v; onFilterChange(); }"
            />
          </span>
        </a-tooltip>
        <a-button @click="refresh" :loading="loading">刷新</a-button>
        <a-popconfirm
          v-if="selectedKeys.length"
          :title="`确认删除选中的 ${selectedKeys.length} 篇文档？`"
          @confirm="onBatchRemove"
        >
          <a-button v-perm="'knowledge:manage'" danger>批量删除（{{ selectedKeys.length }}）</a-button>
        </a-popconfirm>
      </a-space>

      <a-table
        :columns="columns"
        :data-source="list"
        :loading="loading"
        :row-key="(r: any) => r.knowledge_id"
        :row-selection="tableSelection"
        :pagination="{
          current: page.current,
          pageSize: page.pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: ['10', '20', '50'],
          showTotal: (t: number) => `共 ${t} 条`,
        }"
        size="middle"
        @change="onTableChange"
      >
        <template #bodyCell="{ column, record, text }">
          <template v-if="column.key === 'title'">
            <a @click="openDetail(record)">{{ record.title }}</a>
            <a-tag :color="kindColors[record.kind] || 'default'" style="margin-left: 6px">
              {{ kindLabels[record.kind] || record.kind }}
            </a-tag>
            <a-tooltip v-if="record.metadata?.embedded_images" title="文档内嵌图片已由视觉模型生成描述入库（可检索）">
              <a-tag color="gold" style="margin-left: 4px">含图 {{ record.metadata.embedded_images }}</a-tag>
            </a-tooltip>
            <div class="sub">
              {{ record.preview || '（无原文预览）' }}
              <span v-if="record.source_file"> · {{ record.source_file }}</span>
              <a
                v-if="record.source_url"
                :href="record.source_url"
                target="_blank"
                rel="noopener noreferrer"
              > · 原页</a>
            </div>
          </template>
          <template v-else-if="column.key === 'provider'">
            <a-tag :color="record.stale ? 'orange' : 'green'">{{ record.provider }}</a-tag>
            <a-tooltip v-if="!record.has_content" title="入库时未保存原文，无法编辑 / 重建，建议重新上传">
              <a-tag>无原文</a-tag>
            </a-tooltip>
          </template>
          <template v-else-if="column.key === 'chars'">{{ fmtChars(text) }}</template>
          <template v-else-if="column.key === 'updated_at'">{{ formatDate(text) }}</template>
          <template v-else-if="column.key === 'actions'">
            <a-space :size="4">
              <a-button size="small" @click="openDetail(record)">详情</a-button>
              <a-button v-perm="'knowledge:manage'" size="small" @click="openRename(record)">改名</a-button>
              <a-tooltip
                :title="canManage ? '用当前后端配置重新分片并向量化' : permHint('knowledge:manage')"
              >
                <a-button
                  size="small"
                  :disabled="!canManage || !record.has_content"
                  :loading="reindexing === record.knowledge_id"
                  @click="onReindex(record)"
                >重建</a-button>
              </a-tooltip>
              <a-popconfirm title="删除该文档及其全部分片？" @confirm="onRemove(record)">
                <a-button v-perm="'knowledge:manage'" size="small" danger>删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
          <template v-else>{{ text }}</template>
        </template>
        <template #emptyText>
          <a-empty description="知识库为空，点右上角「录入内容」上传文件 / 文本 / 问答对 / URL" />
        </template>
      </a-table>
    </Card>

    <!-- 改名 -->
    <a-modal
      :open="renameModal.visible"
      title="修改文档标题"
      ok-text="保存"
      @ok="onRename"
      @cancel="renameModal.visible = false"
    >
      <a-input v-model:value="renameModal.title" :maxlength="200" placeholder="新的文档标题" />
      <div class="sub" style="margin-top: 8px">改名会同步更新检索结果的来源名称，不会重新向量化</div>
    </a-modal>

    <!-- 详情抽屉 -->
    <a-drawer
      :open="detailVisible"
      :title="detail?.doc?.title || '文档详情'"
      :width="720"
      @close="detailVisible = false"
    >
      <a-spin :spinning="detailLoading">
        <template v-if="detail">
          <a-descriptions :column="2" size="small" bordered>
            <a-descriptions-item label="ID" :span="2">{{ detail.doc.knowledge_id }}</a-descriptions-item>
            <a-descriptions-item label="Provider">{{ detail.doc.provider }}</a-descriptions-item>
            <a-descriptions-item label="录入方式">{{ kindLabels[detail.doc.kind] || detail.doc.kind }}</a-descriptions-item>
            <a-descriptions-item label="分片数">{{ detail.chunk_total }}</a-descriptions-item>
            <a-descriptions-item label="字符数">{{ fmtChars(detail.doc.chars) }}</a-descriptions-item>
            <a-descriptions-item label="向量维度">{{ detail.chunks[0]?.dim ?? '—' }}</a-descriptions-item>
            <a-descriptions-item label="入库时间">{{ formatDate(detail.doc.ingested_at) }}</a-descriptions-item>
            <a-descriptions-item label="更新时间">{{ formatDate(detail.doc.updated_at) }}</a-descriptions-item>
            <a-descriptions-item label="来源文件" :span="2">
              {{ detail.doc.source_file || '—' }}
            </a-descriptions-item>
            <a-descriptions-item v-if="detail.doc.source_url" label="来源链接" :span="2">
              <a :href="detail.doc.source_url" target="_blank" rel="noopener noreferrer">
                {{ detail.doc.source_url }}
              </a>
              <a-tag v-if="detail.doc.metadata?.truncated" color="orange">正文已截断</a-tag>
            </a-descriptions-item>
          </a-descriptions>

          <a-alert
            v-if="detail.doc.stale"
            type="warning"
            show-icon
            style="margin-top: 12px"
            :message="`该文档向量属于 ${detail.doc.provider}，当前默认是 ${stats?.default_provider}，检索不会命中，请重建索引`"
          />

          <div class="section-title" style="margin-top: 16px">正文（编辑后保存将重新分片 + 向量化）</div>
          <a-textarea
            :value="detail.content || ''"
            :rows="8"
            :disabled="!canManage || !detail.doc.has_content"
            :placeholder="detail.doc.has_content ? '' : '入库时未保存原文，无法编辑，请重新上传该文件'"
            @input="onContentInput"
          />
          <a-space style="margin-top: 8px">
            <a-button
              v-perm="'knowledge:manage'"
              type="primary"
              :loading="saving"
              :disabled="!detail.doc.has_content"
              @click="onSaveContent"
            >保存并重建</a-button>
            <a-button
              v-perm="'knowledge:manage'"
              :disabled="!detail.doc.has_content || reindexing === detail.doc.knowledge_id"
              :loading="reindexing === detail.doc.knowledge_id"
              @click="() => detail && onReindex(detail.doc)"
            >仅重建索引</a-button>
          </a-space>

          <template v-if="detail.doc.metadata?.images?.length">
            <div class="section-title" style="margin-top: 16px">
              内嵌图片（{{ detail.doc.metadata.images.length }}<template
                v-if="detail.doc.metadata.embedded_images > detail.doc.metadata.images.length"
              > / 文档共 {{ detail.doc.metadata.embedded_images }} 张，超出上限部分仅保留描述</template>）
            </div>
            <div class="img-gallery">
              <a-image
                v-for="(im, i) in detail.doc.metadata.images"
                :key="i"
                :src="im"
                :width="120"
                :height="120"
                style="object-fit: cover; border-radius: 6px"
              />
            </div>
          </template>

          <div class="section-title" style="margin-top: 16px">
            分片预览（{{ detail.chunks.length }} / {{ detail.chunk_total }}）
          </div>
          <div class="chunk-list">
            <div v-for="c in detail.chunks" :key="c.id" class="chunk">
              <div class="chunk-head">#{{ c.index }} · {{ c.text.length }} 字 · 向量 {{ c.dim }} 维</div>
              <pre>{{ c.text }}</pre>
            </div>
            <a-empty v-if="!detail.chunks.length" description="暂无分片" />
          </div>
        </template>
        <a-empty v-else-if="!detailLoading" description="没有加载到文档" />
      </a-spin>
    </a-drawer>
  </div>
</template>

<style scoped>
.sub {
  font-size: 12px;
  color: var(--app-muted);
  margin-top: 2px;
}
.section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--app-fg);
  margin-bottom: 8px;
}
.chunk-list {
  max-height: 420px;
  overflow: auto;
  background: var(--app-panel-2);
  border: 1px solid var(--app-border);
  border-radius: 6px;
  padding: 8px;
}
.chunk {
  padding: 6px 8px;
  border-bottom: 1px dashed #e5e7eb;
}
.chunk:last-child { border-bottom: none; }
.chunk-head {
  font-size: 12px;
  color: var(--app-muted);
  font-family: 'SF Mono', Consolas, monospace;
}
.chunk pre {
  margin: 4px 0 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  color: var(--app-fg);
}
.img-gallery {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
</style>
