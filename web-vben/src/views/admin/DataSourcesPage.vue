<script setup lang="ts">
/**
 * 数据源管理 —— 登记可供「智能问数」切换分析的外部数据库（MySQL / SQLite / Postgres）。
 *
 * 权限：读需 datasource:read（路由已挡），写 / 删 / 测试需 datasource:manage。
 * 按钮级：写操作按钮统一挂 v-perm（无权置灰 + 悬停说明缺哪条），开关类控件用 a-tooltip 补提示。
 * 安全：连接串含口令，列表只展示掩码串；编辑时留空表示不修改连接串。
 * 「本应用主库」是内置源，无需在此登记即可在问数页选用。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { message } from 'ant-design-vue';
import { datasourceApi } from '@/api';
import { usePerm } from '@/composables/usePerm';
import type { DataSourceItem } from '@/typings';

const { can, tip: permHint } = usePerm();
const canManage = computed(() => can('datasource:manage'));

const list = ref<DataSourceItem[]>([]);
const loading = ref(false);
const saving = ref(false);
const testing = ref(false);

const formOpen = ref(false);
const editingId = ref<string | null>(null);
const form = reactive({
  name: '',
  dialect: 'sqlite',
  url: '',
  readonly: true,
  enabled: true,
  description: '',
});

const deleteTarget = ref<DataSourceItem | null>(null);

const dialectOptions = [
  { label: 'SQLite 文件', value: 'sqlite' },
  { label: 'MySQL / MariaDB', value: 'mysql' },
  { label: 'PostgreSQL', value: 'postgresql' },
];

const urlPlaceholder = computed(() => {
  switch (form.dialect) {
    case 'mysql':
      return 'mysql://用户:口令@主机:3306/库名';
    case 'postgresql':
      return 'postgresql://用户:口令@主机:5432/库名';
    default:
      return 'sqlite:////绝对/路径/data.db（也可直接填文件路径）';
  }
});

const columns = [
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: '类型', key: 'dialect', width: 130 },
  { title: '连接串（已掩码）', key: 'url' },
  { title: '只读', key: 'readonly', width: 80 },
  { title: '启用', key: 'enabled', width: 90 },
  { title: '更新时间', key: 'updated_at', width: 180 },
  { title: '操作', key: 'actions', width: 220 },
];

function dialectLabel(d: string) {
  return dialectOptions.find((o) => o.value === d)?.label || d;
}

function fail(e: unknown, fallback: string) {
  const err = e as { detail?: string; message?: string };
  message.error(err?.detail || err?.message || fallback);
}

async function load() {
  loading.value = true;
  try {
    list.value = await datasourceApi.list();
  } catch (e) {
    list.value = [];
    fail(e, '数据源列表加载失败');
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editingId.value = null;
  Object.assign(form, {
    name: '',
    dialect: 'sqlite',
    url: '',
    readonly: true,
    enabled: true,
    description: '',
  });
  formOpen.value = true;
}

function openEdit(row: DataSourceItem) {
  editingId.value = row.id;
  Object.assign(form, {
    name: row.name,
    dialect: row.dialect,
    url: '', // 编辑默认不改连接串
    readonly: row.readonly,
    enabled: row.enabled,
    description: row.description,
  });
  formOpen.value = true;
}

async function onSave() {
  if (!form.name.trim()) return message.warning('请填写数据源名称');
  if (!editingId.value && !form.url.trim())
    return message.warning('请填写连接串');
  saving.value = true;
  try {
    if (editingId.value) {
      const payload: Partial<typeof form> = {
        name: form.name.trim(),
        dialect: form.dialect,
        readonly: form.readonly,
        enabled: form.enabled,
        description: form.description.trim(),
      };
      if (form.url.trim()) payload.url = form.url.trim();
      await datasourceApi.update(editingId.value, payload);
      message.success('数据源已更新');
    } else {
      await datasourceApi.create({
        name: form.name.trim(),
        dialect: form.dialect,
        url: form.url.trim(),
        readonly: form.readonly,
        enabled: form.enabled,
        description: form.description.trim(),
      });
      message.success('数据源已创建');
    }
    formOpen.value = false;
    await load();
  } catch (e) {
    fail(e, '保存失败');
  } finally {
    saving.value = false;
  }
}

// 测试当前表单草稿的连通性（编辑时带 existing_id，url 留空则测已存的）
async function onTestDraft() {
  if (!form.dialect) return message.warning('请选择类型');
  if (!form.url.trim() && !editingId.value)
    return message.warning('请先填写连接串');
  testing.value = true;
  try {
    const res = await datasourceApi.test({
      dialect: form.dialect,
      url: form.url.trim(),
      existing_id: editingId.value || undefined,
    });
    if (res.ok) {
      const extra = res.table_count >= 0 ? `，可见 ${res.table_count} 张表` : '';
      message.success(`${res.detail}${extra}`);
    } else {
      message.error(res.detail || '连接失败');
    }
  } catch (e) {
    fail(e, '连接测试失败');
  } finally {
    testing.value = false;
  }
}

async function onTestRow(row: DataSourceItem) {
  testing.value = true;
  try {
    const res = await datasourceApi.test({ dialect: row.dialect, url: '', existing_id: row.id });
    if (res.ok) {
      const extra = res.table_count >= 0 ? `，可见 ${res.table_count} 张表` : '';
      message.success(`${res.detail}${extra}`);
    } else {
      message.error(res.detail || '连接失败');
    }
  } catch (e) {
    fail(e, '连接测试失败');
  } finally {
    testing.value = false;
  }
}

async function onToggle(row: DataSourceItem, field: 'enabled' | 'readonly', checked: boolean) {
  try {
    await datasourceApi.update(row.id, { [field]: checked });
    await load();
  } catch (e) {
    fail(e, '更新失败');
    await load();
  }
}

async function onRemove() {
  if (!deleteTarget.value) return;
  try {
    await datasourceApi.remove(deleteTarget.value.id);
    message.success('数据源已删除');
    deleteTarget.value = null;
    await load();
  } catch (e) {
    fail(e, '删除失败');
    deleteTarget.value = null;
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
      description="当前账号只有查看数据源的权限（datasource:read）。新建 / 修改需超级管理员的 datasource:manage。"
    />
    <a-alert
      type="warning"
      show-icon
      style="margin-bottom: 16px"
      message="连接串含数据库口令，请仅登记只读账号；系统对问数强制只读执行且不回传明文连接串。"
    />

    <a-card title="数据源" :bordered="false">
      <template #extra>
        <a-space>
          <a-button @click="load">刷新</a-button>
          <a-button v-perm="'datasource:manage'" type="primary" @click="openCreate">新建数据源</a-button>
        </a-space>
      </template>
      <a-table
        :data-source="list"
        :columns="columns"
        :loading="loading"
        row-key="id"
        size="middle"
        :pagination="{ pageSize: 10, hideOnSinglePage: true }"
      >
        <!-- ant-design-vue 4 只支持 #bodyCell 这一个单元格插槽，按 column.key 分支渲染 -->
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'dialect'">
            <a-tag color="geekblue">{{ dialectLabel(record.dialect) }}</a-tag>
          </template>
          <template v-else-if="column.key === 'url'">
            <code class="url-masked">{{ record.url_masked }}</code>
          </template>
          <template v-else-if="column.key === 'readonly'">
            <a-tooltip :title="canManage ? '' : permHint('datasource:manage')">
              <a-switch
                :checked="record.readonly"
                :disabled="!canManage"
                size="small"
                @change="(c: boolean) => onToggle(record, 'readonly', c)"
              />
            </a-tooltip>
          </template>
          <template v-else-if="column.key === 'enabled'">
            <a-tooltip :title="canManage ? '' : permHint('datasource:manage')">
              <a-switch
                :checked="record.enabled"
                :disabled="!canManage"
                size="small"
                checked-children="启"
                un-checked-children="停"
                @change="(c: boolean) => onToggle(record, 'enabled', c)"
              />
            </a-tooltip>
          </template>
          <template v-else-if="column.key === 'updated_at'">
            {{ record.updated_at ? new Date(record.updated_at * 1000).toLocaleString() : '-' }}
          </template>
          <template v-else-if="column.key === 'actions'">
            <a-space wrap>
              <a-button size="small" v-perm="'datasource:manage'" @click="onTestRow(record)">测试</a-button>
              <a-button size="small" v-perm="'datasource:manage'" @click="openEdit(record)">编辑</a-button>
              <a-button
                size="small"
                danger
                v-perm="'datasource:manage'"
                @click="deleteTarget = record"
              >
                删除
              </a-button>
            </a-space>
          </template>
        </template>
      </a-table>
    </a-card>

    <a-modal
      v-model:open="formOpen"
      :title="editingId ? '编辑数据源' : '新建数据源'"
      :confirm-loading="saving"
      width="640px"
      @ok="onSave"
    >
      <a-form layout="vertical">
        <a-form-item label="名称" required>
          <a-input v-model:value="form.name" placeholder="展示名，全局唯一，如「订单库」" />
        </a-form-item>
        <a-form-item label="类型" required>
          <a-select v-model:value="form.dialect" :options="dialectOptions" />
        </a-form-item>
        <a-form-item label="连接串" :required="!editingId">
          <a-input-password
            v-model:value="form.url"
            :placeholder="editingId ? '留空表示不修改已保存的连接串' : urlPlaceholder"
            autocomplete="new-password"
          />
          <div class="hint">
            同步驱动会自动补成异步驱动（mysql→aiomysql、sqlite→aiosqlite）。
            <template v-if="editingId">留空则沿用原连接串。</template>
          </div>
        </a-form-item>
        <a-form-item label="说明">
          <a-input v-model:value="form.description" placeholder="可选，备注这个库是干什么的" />
        </a-form-item>
        <a-space>
          <a-checkbox v-model:checked="form.readonly">强制只读执行</a-checkbox>
          <a-checkbox v-model:checked="form.enabled">启用（可在问数页选择）</a-checkbox>
        </a-space>
        <div style="margin-top: 16px">
          <a-button :loading="testing" v-perm="'datasource:manage'" @click="onTestDraft">测试连接</a-button>
          <span class="hint" style="margin-left: 8px">保存前可先验证连通性与可见表数量。</span>
        </div>
      </a-form>
    </a-modal>

    <a-modal
      :open="!!deleteTarget"
      title="删除数据源"
      ok-text="删除"
      :ok-button-props="{ danger: true }"
      @cancel="deleteTarget = null"
      @ok="onRemove"
    >
      <p>确定删除数据源【{{ deleteTarget?.name }}】？问数页将不再能选择它。</p>
    </a-modal>
  </div>
</template>

<style scoped>
.url-masked {
  font-size: 12px;
  color: var(--app-muted);
  word-break: break-all;
}
.hint {
  margin-top: 4px;
  color: var(--app-muted);
  font-size: 12px;
}
</style>
