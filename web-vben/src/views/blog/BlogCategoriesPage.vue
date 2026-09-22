<script setup lang="ts">
/**
 * 分类管理 —— 博客文章的分类维护（配套文章管理页 /blog-posts）。
 *
 * 权限：读需 blog:read（路由已挡），增删改需 blog:manage，复用博客两个码、不新增权限。
 *
 * 规则与后端服务层对齐：名称唯一；slug 留空由名称生成（纯中文剥不出 ASCII 时随机兜底）；
 * 分类下还有文章时删除会被 400 拒绝（中文原因直接透出），需先在文章里重新归类。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { message } from 'ant-design-vue';
import dayjs from 'dayjs';
import { blogCategoryApi } from '@/api';
import { usePerm } from '@/composables/usePerm';
import type { BlogCategoryItem } from '@/typings';

const { can, tip: permHint, need } = usePerm();
const canManage = computed(() => can('blog:manage'));

/** 写操作兜底闸门（按钮已按权限置灰，这里拦的是回车提交与权限中途被收） */
function guardManage() {
  if (canManage.value) return false;
  message.warning(need('blog:manage'));
  return true;
}

const list = ref<BlogCategoryItem[]>([]);
const loading = ref(false);

const columns = [
  { title: '名称', key: 'name' },
  { title: 'slug', key: 'slug', width: 180 },
  { title: '文章数', key: 'count', width: 90 },
  { title: '排序', key: 'sort_order', width: 80 },
  { title: '更新时间', key: 'updated_at', width: 170 },
  { title: '操作', key: 'actions', width: 170 },
];

function fmt(ts?: number | null) {
  return ts ? dayjs.unix(ts).format('YYYY-MM-DD HH:mm') : '-';
}

function fail(e: unknown, fallback: string) {
  const err = e as { detail?: string; message?: string };
  message.error(err?.detail || err?.message || fallback);
}

async function load() {
  loading.value = true;
  try {
    const res = await blogCategoryApi.list();
    list.value = res.items;
  } catch (e) {
    fail(e, '分类列表加载失败');
  } finally {
    loading.value = false;
  }
}

// ---------------- 新建 / 编辑（字段少，用弹窗就够） ----------------

const formOpen = ref(false);
const saving = ref(false);
const editingId = ref<string | null>(null);
const form = reactive({ name: '', slug: '', sort_order: 0 });

function openCreate() {
  if (guardManage()) return;
  editingId.value = null;
  Object.assign(form, { name: '', slug: '', sort_order: list.value.length ? Math.max(...list.value.map((c) => c.sort_order)) + 1 : 0 });
  formOpen.value = true;
}

function openEdit(row: BlogCategoryItem) {
  if (guardManage()) return;
  editingId.value = row.id;
  Object.assign(form, { name: row.name, slug: row.slug, sort_order: row.sort_order });
  formOpen.value = true;
}

async function onSave() {
  if (guardManage()) return;
  if (!form.name.trim()) return message.warning('请填写分类名称');
  saving.value = true;
  try {
    const body = {
      name: form.name.trim(),
      slug: form.slug.trim() || null,
      sort_order: form.sort_order,
    };
    if (editingId.value) {
      await blogCategoryApi.update(editingId.value, body);
      message.success('分类已更新');
    } else {
      await blogCategoryApi.create(body);
      message.success('分类已创建');
    }
    formOpen.value = false;
    await load();
  } catch (e) {
    fail(e, '保存失败');
  } finally {
    saving.value = false;
  }
}

async function onRemove(row: BlogCategoryItem) {
  if (guardManage()) return;
  try {
    await blogCategoryApi.remove(row.id);
    message.success('分类已删除');
    await load();
  } catch (e) {
    fail(e, '删除失败');
  }
}

onMounted(load);
</script>

<template>
  <div class="page-card">
    <a-alert
      v-if="!canManage"
      type="info"
      show-icon
      style="margin-bottom: 16px"
      message="只读模式"
      description="当前账号只有查看博客的权限（blog:read）。新建 / 编辑 / 删除分类需 blog:manage。"
    />

    <a-card title="分类" :bordered="false">
      <template #extra>
        <a-space>
          <a-button @click="load">刷新</a-button>
          <a-button v-perm="'blog:manage'" type="primary" @click="openCreate">新建分类</a-button>
        </a-space>
      </template>

      <a-table
        :data-source="list"
        :columns="columns"
        :loading="loading"
        row-key="id"
        size="middle"
        :pagination="false"
      >
        <!-- ant-design-vue 4 只支持 #bodyCell 这一个单元格插槽，按 column.key 分支渲染 -->
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <span style="font-weight: 600">{{ record.name }}</span>
          </template>
          <template v-else-if="column.key === 'slug'">
            <code>{{ record.slug }}</code>
          </template>
          <template v-else-if="column.key === 'count'">
            <a-tag :color="record.post_count ? 'blue' : 'default'">{{ record.post_count }}</a-tag>
          </template>
          <template v-else-if="column.key === 'sort_order'">{{ record.sort_order }}</template>
          <template v-else-if="column.key === 'updated_at'">{{ fmt(record.updated_at) }}</template>
          <template v-else-if="column.key === 'actions'">
            <a-space :size="4">
              <a-tooltip :title="canManage ? '' : permHint('blog:manage')">
                <a-button size="small" :disabled="!canManage" @click="openEdit(record)">编辑</a-button>
              </a-tooltip>
              <a-popconfirm
                :title="record.post_count ? `该分类下还有 ${record.post_count} 篇文章，需先重新归类` : '删除后不可恢复，确定？'"
                :ok-button-props="{ disabled: !!record.post_count }"
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

    <a-modal
      v-model:open="formOpen"
      :title="editingId ? '编辑分类' : '新建分类'"
      :confirm-loading="saving"
      :ok-button-props="{ disabled: !canManage }"
      @ok="onSave"
    >
      <a-form layout="vertical" style="margin-top: 12px">
        <a-form-item label="名称" required>
          <a-input v-model:value="form.name" :maxlength="50" placeholder="例如：技术笔记" />
        </a-form-item>
        <a-form-item>
          <template #label>
            slug
            <span style="color: var(--text-muted, #999)">（留空自动生成；仅支持小写字母 / 数字 / 连字符）</span>
          </template>
          <a-input v-model:value="form.slug" :maxlength="80" placeholder="例如 tech" />
        </a-form-item>
        <a-form-item>
          <template #label>
            排序
            <span style="color: var(--text-muted, #999)">（前台分类导航按此升序展示）</span>
          </template>
          <a-input-number v-model:value="form.sort_order" :min="0" style="width: 160px" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>
