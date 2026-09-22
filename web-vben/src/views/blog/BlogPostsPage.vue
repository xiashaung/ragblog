<script setup lang="ts">
/**
 * 文章管理 —— 个人博客的后台入口（配套前台在 /blog，独立项目 web-blog/）。
 *
 * 权限：读需 blog:read（路由已挡），新建 / 编辑 / 发布 / 下线 / 删除需 blog:manage。
 * 按钮级门控沿用两套范式：独立按钮挂 v-perm；已在 tooltip 里的按钮用 :disabled + 动态 title。
 *
 * 「发布」不是改个状态：后端会把正文同步进知识库（调嵌入模型重建该文章的索引），
 * 耗时与正文长度成正比；下线 / 删除会连带摘掉索引。已发布文章编辑保存后若改了
 * 标题或正文会自动重同步，无需再点一次发布。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { message } from 'ant-design-vue';
import dayjs from 'dayjs';
import { blogApi, blogCategoryApi } from '@/api';
import { usePerm } from '@/composables/usePerm';
import MarkdownEditor from '@/components/MarkdownEditor.vue';
import type { BlogCategoryItem, BlogPostItem } from '@/typings';

const { can, tip: permHint, need } = usePerm();
const canManage = computed(() => can('blog:manage'));

/** 写操作兜底闸门（按钮已按权限置灰，这里拦的是回车提交与权限中途被收） */
function guardManage() {
  if (canManage.value) return false;
  message.warning(need('blog:manage'));
  return true;
}

// ---------------- 列表 ----------------

const list = ref<BlogPostItem[]>([]);
const total = ref(0);
const loading = ref(false);
const query = reactive({
  keyword: '',
  status: undefined as string | undefined,
  categoryId: undefined as string | undefined,
  page: 1,
  pageSize: 10,
});

// 分类选项：筛选下拉与抽屉表单共用（数据量小，进页一次拉全）
const categories = ref<BlogCategoryItem[]>([]);
const categoryOptions = computed(() =>
  categories.value.map((c) => ({ label: `${c.name}（${c.post_count}）`, value: c.id })),
);

async function loadCategories() {
  try {
    categories.value = (await blogCategoryApi.list()).items;
  } catch {
    /* 分类拉失败不拦列表，下拉空着而已 */
  }
}

const columns = [
  { title: '标题', key: 'title' },
  { title: 'slug', key: 'slug', width: 200 },
  { title: '分类', key: 'category', width: 120 },
  { title: '状态', key: 'status', width: 110 },
  { title: '阅读', key: 'views', width: 80 },
  { title: '更新时间', key: 'updated_at', width: 170 },
  { title: '操作', key: 'actions', width: 300 },
];

function fmt(ts?: number | null) {
  return ts ? dayjs.unix(ts).format('YYYY-MM-DD HH:mm') : '-';
}

async function load() {
  loading.value = true;
  try {
    const res = await blogApi.list({
      keyword: query.keyword.trim() || undefined,
      status: query.status,
      category_id: query.categoryId,
      offset: (query.page - 1) * query.pageSize,
      limit: query.pageSize,
    });
    list.value = res.items;
    total.value = res.total;
  } catch (e) {
    fail(e, '文章列表加载失败');
  } finally {
    loading.value = false;
  }
}

function onSearch() {
  query.page = 1;
  load();
}

function onPage(page: number, size: number) {
  query.page = page;
  query.pageSize = size;
  load();
}

function fail(e: unknown, fallback: string) {
  const err = e as { detail?: string; message?: string };
  message.error(err?.detail || err?.message || fallback);
}

// ---------------- 编辑抽屉 ----------------

const formOpen = ref(false);
const saving = ref(false);
const editingId = ref<string | null>(null);
const editingStatus = ref<'draft' | 'published'>('draft');
const form = reactive({
  title: '',
  slug: '',
  summary: '',
  cover_url: '',
  tags: [] as string[],
  categoryId: undefined as string | undefined,
  featured: false,
  content: '',
});

function openCreate() {
  if (guardManage()) return;
  editingId.value = null;
  editingStatus.value = 'draft';
  Object.assign(form, { title: '', slug: '', summary: '', cover_url: '', tags: [], categoryId: undefined, featured: false, content: '' });
  formOpen.value = true;
}

async function openEdit(row: BlogPostItem) {
  if (formOpen.value) return;
  try {
    // 列表项不带正文，编辑前先取一次详情
    const detail = await blogApi.detail(row.id);
    editingId.value = detail.id;
    editingStatus.value = detail.status;
    Object.assign(form, {
      title: detail.title,
      slug: detail.slug,
      summary: detail.summary,
      cover_url: detail.cover_url || '',
      tags: [...(detail.tags || [])],
      categoryId: detail.category_id ?? undefined,
      featured: detail.featured,
      content: detail.content || '',
    });
    formOpen.value = true;
  } catch (e) {
    fail(e, '文章详情加载失败');
  }
}

async function onSave() {
  if (guardManage()) return;
  if (!form.title.trim()) return message.warning('请填写标题');
  saving.value = true;
  try {
    const body = {
      title: form.title.trim(),
      slug: form.slug.trim() || null,
      summary: form.summary.trim(),
      cover_url: form.cover_url.trim() || null,
      tags: form.tags,
      // 表单始终反映真实归属：undefined 归一为 null（= 移出分类），不会漏传
      category_id: form.categoryId ?? null,
      featured: form.featured,
      content: form.content,
    };
    if (editingId.value) {
      await blogApi.update(editingId.value, body);
      message.success(
        editingStatus.value === 'published' ? '文章已更新（AI 索引已同步）' : '文章已更新',
      );
    } else {
      await blogApi.create(body);
      message.success('草稿已创建');
    }
    formOpen.value = false;
    await load();
  } catch (e) {
    fail(e, '保存失败');
  } finally {
    saving.value = false;
  }
}

// ---------------- 发布 / 下线 / 删除 ----------------

const busyId = ref<string | null>(null);

async function onPublish(row: BlogPostItem) {
  if (guardManage()) return;
  busyId.value = row.id;
  try {
    await blogApi.publish(row.id);
    message.success('已发布：前台可见，AI 索引已重建');
    await load();
  } catch (e) {
    fail(e, '发布失败');
  } finally {
    busyId.value = null;
  }
}

async function onUnpublish(row: BlogPostItem) {
  if (guardManage()) return;
  busyId.value = row.id;
  try {
    await blogApi.unpublish(row.id);
    message.success('已下线：回到草稿箱');
    await load();
  } catch (e) {
    fail(e, '下线失败');
  } finally {
    busyId.value = null;
  }
}

async function onRemove(row: BlogPostItem) {
  if (guardManage()) return;
  try {
    await blogApi.remove(row.id);
    message.success('文章已删除（含 AI 索引）');
    await load();
  } catch (e) {
    fail(e, '删除失败');
  }
}

/** 前台预览：博客 SPA 挂在 /blog 下（构建产物存在时才可访问） */
function onPreview(row: BlogPostItem) {
  window.open(`/blog/post/${row.slug}`, '_blank');
}

onMounted(() => {
  load();
  loadCategories();
});
</script>

<template>
  <div class="page-card">
    <a-alert
      v-if="!canManage"
      type="info"
      show-icon
      style="margin-bottom: 16px"
      message="只读模式"
      description="当前账号只有查看博客的权限（blog:read）。新建 / 编辑 / 发布需 blog:manage。"
    />

    <a-card title="文章" :bordered="false">
      <template #extra>
        <a-space>
          <a-input
            v-model:value="query.keyword"
            placeholder="标题 / 摘要关键字"
            allow-clear
            style="width: 200px"
            @press-enter="onSearch"
          />
          <a-select
            v-model:value="query.status"
            placeholder="状态"
            allow-clear
            style="width: 120px"
            :options="[
              { label: '草稿', value: 'draft' },
              { label: '已发布', value: 'published' },
            ]"
            @change="onSearch"
          />
          <a-select
            v-model:value="query.categoryId"
            placeholder="分类"
            allow-clear
            style="width: 160px"
            :options="categoryOptions"
            @change="onSearch"
          />
          <a-button @click="load">刷新</a-button>
          <a-button v-perm="'blog:manage'" type="primary" @click="openCreate">新建文章</a-button>
        </a-space>
      </template>

      <a-table
        :data-source="list"
        :columns="columns"
        :loading="loading"
        row-key="id"
        size="middle"
        :pagination="{
          current: query.page,
          pageSize: query.pageSize,
          total,
          onChange: onPage,
          showSizeChanger: false,
        }"
      >
        <!-- ant-design-vue 4 只支持 #bodyCell 这一个单元格插槽，按 column.key 分支渲染 -->
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'title'">
            <div>
              <a-tag v-if="record.featured" color="gold">置顶</a-tag>
              <a-tooltip
                v-if="record.status === 'published' && !record.knowledge_id"
                title="已发布但 AI 索引缺失（可能在知识库页被删过），重新发布即可修复"
              >
                <a-tag color="orange">缺索引</a-tag>
              </a-tooltip>
              <a @click="openEdit(record)">{{ record.title }}</a>
            </div>
            <div>
              <a-tag v-for="t in record.tags" :key="t" style="margin-inline-end: 4px">{{ t }}</a-tag>
            </div>
          </template>
          <template v-else-if="column.key === 'slug'">
            <code>{{ record.slug }}</code>
          </template>
          <template v-else-if="column.key === 'category'">
            <a-tag v-if="record.category" color="blue">{{ record.category.name }}</a-tag>
            <span v-else style="color: var(--text-muted, #999)">未分类</span>
          </template>
          <template v-else-if="column.key === 'status'">
            <a-tag :color="record.status === 'published' ? 'green' : 'default'">
              {{ record.status === 'published' ? '已发布' : '草稿' }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'views'">{{ record.view_count }}</template>
          <template v-else-if="column.key === 'updated_at'">{{ fmt(record.updated_at) }}</template>
          <template v-else-if="column.key === 'actions'">
            <a-space :size="4">
              <a-button size="small" @click="openEdit(record)">编辑</a-button>
              <a-button
                v-if="record.status !== 'published'"
                size="small"
                type="primary"
                :loading="busyId === record.id"
                v-perm="'blog:manage'"
                @click="onPublish(record)"
              >
                发布
              </a-button>
              <a-button v-else size="small" :loading="busyId === record.id" v-perm="'blog:manage'" @click="onUnpublish(record)">
                下线
              </a-button>
              <a-button size="small" @click="onPreview(record)">预览</a-button>
              <a-popconfirm
                title="删除后正文与 AI 索引一并移除，确定？"
                ok-text="删除"
                cancel-text="取消"
                @confirm="onRemove(record)"
              >
                <a-button size="small" danger v-perm="'blog:manage'">删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
        </template>
      </a-table>
    </a-card>

    <!-- 编辑抽屉：字段多、正文长，弹窗放不下 -->
    <a-drawer
      v-model:open="formOpen"
      :title="editingId ? `编辑文章（${editingStatus === 'published' ? '已发布，保存会同步索引' : '草稿'}）` : '新建文章'"
      width="72%"
      :body-style="{ paddingBottom: '80px' }"
    >
      <a-form layout="vertical">
        <a-row :gutter="16">
          <a-col :span="8">
            <a-form-item label="标题" required>
              <a-input v-model:value="form.title" :maxlength="200" placeholder="文章标题" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item>
              <template #label>
                slug
                <span style="color: var(--text-muted, #999)">（留空自动生成；仅支持小写字母 / 数字 / 连字符）</span>
              </template>
              <a-input v-model:value="form.slug" :maxlength="150" placeholder="例如 redis-cache-notes" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item>
              <template #label>
                分类
                <span style="color: var(--text-muted, #999)">（去管理页建；清空 = 未分类）</span>
              </template>
              <a-select
                v-model:value="form.categoryId"
                placeholder="未分类"
                allow-clear
                :options="categoryOptions"
              />
            </a-form-item>
          </a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item>
              <template #label>标签 <span style="color: var(--text-muted, #999)">（回车创建，最多 10 个）</span></template>
              <a-select v-model:value="form.tags" mode="tags" :maxlength="30" placeholder="redis" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="封面图 URL">
              <a-input v-model:value="form.cover_url" placeholder="先在正文里插图，再复制 /media/... 地址" />
            </a-form-item>
          </a-col>
          <a-col :span="4">
            <a-form-item label="置顶">
              <a-switch v-model:checked="form.featured" />
            </a-form-item>
          </a-col>
        </a-row>
        <a-form-item>
          <template #label>摘要 <span style="color: var(--text-muted, #999)">（留空则从正文自动摘取前 120 字）</span></template>
          <a-textarea v-model:value="form.summary" :maxlength="500" :rows="2" show-count />
        </a-form-item>
        <a-form-item label="正文（Markdown）">
          <MarkdownEditor v-model="form.content" :rows="18" placeholder="支持工具栏排版与插图，图片会落盘到 /media 后以地址插入" />
        </a-form-item>
      </a-form>
      <div style="position: absolute; inset-block-end: 0; inset-inline-end: 0; padding: 12px 16px; border-block-start: 1px solid rgba(0, 0, 0, 0.06); width: 100%">
        <a-space style="float: inline-end">
          <a-button @click="formOpen = false">取消</a-button>
          <a-tooltip :title="canManage ? '' : permHint('blog:manage')">
            <a-button type="primary" :loading="saving" :disabled="!canManage" @click="onSave">保存</a-button>
          </a-tooltip>
        </a-space>
      </div>
    </a-drawer>
  </div>
</template>
