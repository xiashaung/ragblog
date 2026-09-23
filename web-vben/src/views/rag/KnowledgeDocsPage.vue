<script setup lang="ts">
/**
 * 知识库 · 文档列表 —— 概览统计 + 文档列表（搜索 / 过滤 / 分页 / 批量删除）、
 * 详情与分片查看、改名 / 改内容 / 重建索引 / 删除。
 * 内容录入（文件 / 文本 / 问答对 / URL）已拆到独立「内容录入」页。
 * 向量化模型由后端 .env 配置决定，前端不选择平台。
 *
 * 按钮级：写操作按钮挂 v-perm / :disabled，弹窗「保存」按权限禁用，
 * 处理函数里再留一道 guard；只读用户看到的空状态与正文标题也跟着权限换文案。
 *
 * 正文默认是渲染态（Markdown 文档按语法渲染，纯文本按原样展示），
 * 点「编辑源码」才切进编辑器；保存时把当前格式一并送回后端（不传则沿用原格式）。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
// 模板里用的是 PascalCase <Card>，全局注册只提供 a-*，必须显式引入
import { Card, message } from 'ant-design-vue';
import { ragApi } from '@/api';
import MarkdownEditor from '@/components/MarkdownEditor.vue';
import RichText from '@/components/RichText.vue';
import { usePerm } from '@/composables/usePerm';
import { useBusinessStore } from '@/store/modules/business';
import type { ContentFormat, KnowledgeDetail, KnowledgeDocItem, KnowledgeStats } from '@/typings';

const router = useRouter();
const route = useRoute();
const business = useBusinessStore();
// 按钮级权限：改名 / 重建 / 删除 / 保存正文都是写操作，统一要 knowledge:manage。
// 能进这一页说明至少有 knowledge:read（路由守卫已挡），所以只需再区分读写。
const { can, tip: permHint, need, okProps } = usePerm();
const canManage = computed(() => can('knowledge:manage'));

/** 写操作兜底闸门（按钮已按权限置灰，这里拦的是回车提交与权限中途被收） */
function guardManage() {
  if (canManage.value) return false;
  message.warning(need('knowledge:manage'));
  return true;
}

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

// 窄屏（笔记本）兜底：「标题」列里带标签与预览摘要，不给最小宽度会被挤成一堆折行，
// 因此写明列宽 + scroll.x（横向滚动代替列内容变形），操作列钉右。
const columns = [
  { title: '标题', dataIndex: 'title', key: 'title', width: 320 },
  { title: 'Provider', dataIndex: 'provider', key: 'provider', width: 150 },
  { title: '分片', dataIndex: 'chunks', key: 'chunks', width: 70 },
  { title: '字符', dataIndex: 'chars', key: 'chars', width: 90 },
  { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at', width: 160, responsive: ['lg'] as const },
  { title: '操作', key: 'actions', width: 300, fixed: 'right' as const },
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
/** 正文区默认只读渲染，点「编辑源码」才切成编辑器 */
const bodyEditing = ref(false);
/** 编辑中的目标格式：允许把一篇纯文本文档改写成 Markdown（反过来也行） */
const editFormat = ref<ContentFormat>('markdown');

async function openDetail(row: KnowledgeDocItem) {
  detailVisible.value = true;
  detailLoading.value = true;
  detail.value = null;
  bodyEditing.value = false;
  try {
    const data = await ragApi.detail(row.knowledge_id, 100);
    detail.value = data;
    editContent.value = data?.content || '';
    editFormat.value = data?.doc?.format === 'markdown' ? 'markdown' : 'text';
  } catch (e: any) {
    message.error(e?.detail || e?.message || '加载文档详情失败');
  } finally {
    detailLoading.value = false;
  }
}

/** 放弃未保存的改动，回到只读渲染态 */
function cancelEditBody() {
  editContent.value = detail.value?.content || '';
  editFormat.value = detail.value?.doc?.format === 'markdown' ? 'markdown' : 'text';
  bodyEditing.value = false;
}

// 保存正文 = 重新分片 + 重新向量化（成本与一次入库相同）
async function onSaveContent() {
  if (guardManage()) return;
  const doc = detail.value?.doc;
  if (!doc) return;
  if (!editContent.value.trim()) return message.warning('正文不能为空');
  saving.value = true;
  try {
    const updated = await ragApi.update(doc.knowledge_id, {
      content: editContent.value,
      format: editFormat.value,
    });
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
  if (guardManage()) return;
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
  if (guardManage()) return;
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
  if (guardManage()) return;
  try {
    await ragApi.remove(row.knowledge_id);
    message.success('已删除');
    await refresh();
  } catch (e: any) {
    message.error(e?.detail || e?.message || '删除失败');
  }
}

async function onBatchRemove() {
  if (guardManage()) return;
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
        :scroll="{ x: 1120 }"
        @change="onTableChange"
      >
        <template #bodyCell="{ column, record, text }">
          <template v-if="column.key === 'title'">
            <a @click="openDetail(record)">{{ record.title }}</a>
            <a-tag :color="kindColors[record.kind] || 'default'" style="margin-left: 6px">
              {{ kindLabels[record.kind] || record.kind }}
            </a-tag>
            <a-tooltip v-if="record.format === 'markdown'" title="正文是 Markdown 源码，详情里按语法渲染">
              <a-tag color="geekblue" style="margin-left: 4px">MD</a-tag>
            </a-tooltip>
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
          <!-- 无权的人看到「点右上角录入」只会去找那个灰色按钮，换成指路给管理员 -->
          <a-empty
            :description="
              canManage
                ? '知识库为空，点右上角「录入内容」上传文件 / 文本 / 问答对 / URL'
                : '知识库还没有内容，需要录入请联系管理员开通 knowledge:manage'
            "
          />
        </template>
      </a-table>
    </Card>

    <!-- 改名 -->
    <a-modal
      :open="renameModal.visible"
      title="修改文档标题"
      ok-text="保存"
      :ok-button-props="okProps('knowledge:manage')"
      @ok="onRename"
      @cancel="renameModal.visible = false"
    >
      <a-input v-model:value="renameModal.title" :maxlength="200" placeholder="新的文档标题" />
      <div class="sub" style="margin-top: 8px">改名会同步更新检索结果的来源名称，不会重新向量化</div>
    </a-modal>

    <!-- 详情抽屉：窄屏（笔记本）下不让它固定 860 把整页遮完，取容器宽与 92vw 的较小值 -->
    <a-drawer
      :open="detailVisible"
      :title="detail?.doc?.title || '文档详情'"
      width="min(860px, 94vw)"
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
            :message="`该文档向量属于 ${detail.doc.provider}，当前默认是 ${stats?.default_provider}，检索不会命中${canManage ? '，请点「重建」重建索引' : '，需要重建请联系管理员'}`"
          />

          <div class="section-title" style="margin-top: 16px">
            正文
            <a-tag :color="detail.doc.format === 'markdown' ? 'geekblue' : 'default'">
              {{ detail.doc.format === 'markdown' ? 'Markdown' : '纯文本' }}
            </a-tag>
            <a-button
              v-if="canManage && detail.doc.has_content && !bodyEditing"
              type="link"
              size="small"
              @click="bodyEditing = true"
            >编辑源码</a-button>
            <a-button v-else-if="bodyEditing" type="link" size="small" @click="cancelEditBody">回到预览</a-button>
          </div>

          <template v-if="bodyEditing">
            <a-radio-group
              v-model:value="editFormat"
              :options="[
                { label: '纯文本', value: 'text' },
                { label: 'Markdown', value: 'markdown' },
              ]"
              option-type="button"
              button-style="solid"
              size="small"
            />
            <div class="sub" style="margin: 4px 0 8px">
              切换格式只是改「后端怎么解释这段正文」，保存后会按新格式渲染；改动正文会重新分片 + 向量化
            </div>
            <MarkdownEditor v-model="editContent" :format="editFormat" :rows="12" default-view="split" />
            <a-space style="margin-top: 8px">
              <a-button
                v-perm="'knowledge:manage'"
                type="primary"
                :loading="saving"
                @click="onSaveContent"
              >保存并重建</a-button>
              <a-button :disabled="saving" @click="cancelEditBody">取消</a-button>
            </a-space>
          </template>
          <template v-else>
            <a-alert
              v-if="!detail.doc.has_content"
              type="warning"
              show-icon
              message="入库时未保存原文，无法编辑 / 重建，请重新上传该文件"
            />
            <!-- Markdown 文档按语法渲染，纯文本保持原样：否则历史文档里的 # | 会被误读成排版 -->
            <RichText v-else-if="detail.doc.format === 'markdown'" :text="detail.content || ''" class="doc-body" />
            <pre v-else class="doc-plain">{{ detail.content || '（正文为空）' }}</pre>
          </template>
          <a-space v-if="!bodyEditing" style="margin-top: 8px">
            <a-button
              v-perm="'knowledge:manage'"
              type="primary"
              :loading="saving"
              :disabled="!detail.doc.has_content"
              @click="bodyEditing = true"
            >编辑正文</a-button>
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
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: var(--app-fg);
  margin-bottom: 8px;
}
/* 渲染态正文：跟抽屉边框留点呼吸空间，长表 / 长代码块自己滚动 */
.doc-body {
  border: 1px solid var(--app-border);
  border-radius: 6px;
  padding: 10px 12px;
  max-height: 320px;
  overflow: auto;
  background: var(--app-panel);
}
.doc-plain {
  margin: 0;
  padding: 10px 12px;
  max-height: 320px;
  overflow: auto;
  border: 1px solid var(--app-border);
  border-radius: 6px;
  background: var(--app-panel-2);
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
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
  border-bottom: 1px dashed var(--app-border);
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
