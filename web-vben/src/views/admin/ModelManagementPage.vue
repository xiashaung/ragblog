<script setup lang="ts">
/**
 * 模型管理 —— 模型提供者配置的增删改查。
 *
 * 权限：读需 system:read（路由已挡），写需 system:manage。
 * 请求走默认导出的 http 实例（baseURL=/api/v1），与后端 system_config 路由挂载点一致；
 * envelope 由拦截器拆掉，页面直接读 res.data。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { message } from 'ant-design-vue';
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons-vue';
import http from '@/api/request';
import { useAuthStore } from '@/store/modules/auth';

const auth = useAuthStore();
const canManage = computed(() => auth.hasPerm('system:manage'));

const loading = ref(false);
const saving = ref(false);

// 模型提供者配置接口
interface ProviderConfig {
  provider: string;
  name: string;
  base_url: string;
  api_key: string;
  chat_model: string;
  embedding_model: string;
  rerank_model: string;
}

// 所有提供者配置列表
const providerList = ref<ProviderConfig[]>([]);

// 新增/编辑表单
const showModal = ref(false);
const editingProvider = ref('');
const formState = reactive({
  provider: '',
  name: '',
  base_url: '',
  api_key: '',
  chat_model: '',
  embedding_model: '',
  rerank_model: '',
});

// 表格列定义
const tableColumns = [
  { title: 'Provider', dataIndex: 'provider', key: 'provider', width: 120 },
  { title: '名称', dataIndex: 'name', key: 'name', width: 150 },
  { title: '服务地址', dataIndex: 'base_url', key: 'base_url', width: 260, ellipsis: true },
  { title: '对话模型', dataIndex: 'chat_model', key: 'chat_model', width: 180, ellipsis: true },
  { title: '向量模型', dataIndex: 'embedding_model', key: 'embedding_model', width: 180, ellipsis: true },
  { title: '重排序模型', dataIndex: 'rerank_model', key: 'rerank_model', width: 180, ellipsis: true },
  { title: '操作', key: 'actions', width: 130, fixed: 'right' as const },
];

// 加载提供者配置
async function loadProviderConfigs() {
  loading.value = true;
  try {
    const res: any = await http.get('/system/config/providers');
    const data = res.data ?? {};
    providerList.value = Object.entries(data).map(([provider, cfg]: [string, any]) => ({
      provider,
      name: cfg?.name ?? provider,
      base_url: cfg?.base_url ?? '',
      api_key: cfg?.api_key ?? '',
      chat_model: cfg?.chat_model ?? '',
      embedding_model: cfg?.embedding_model ?? '',
      rerank_model: cfg?.rerank_model ?? '',
    }));
  } catch (e: any) {
    console.error('加载模型提供者配置失败:', e);
    message.error(e?.detail || '加载配置失败');
  } finally {
    loading.value = false;
  }
}

// 打开新增对话框
function openCreateModal() {
  editingProvider.value = '';
  Object.assign(formState, {
    provider: '',
    name: '',
    base_url: '',
    api_key: '',
    chat_model: '',
    embedding_model: '',
    rerank_model: '',
  });
  showModal.value = true;
}

// 打开编辑对话框
function openEditModal(record: ProviderConfig) {
  editingProvider.value = record.provider;
  Object.assign(formState, {
    provider: record.provider,
    name: record.name,
    base_url: record.base_url,
    api_key: record.api_key,
    chat_model: record.chat_model,
    embedding_model: record.embedding_model,
    rerank_model: record.rerank_model,
  });
  showModal.value = true;
}

// 保存配置（新增或更新）
async function saveProvider() {
  if (!canManage.value) {
    message.warning('无修改权限');
    return;
  }

  if (!editingProvider.value) {
    if (!formState.provider.trim()) {
      message.error('请输入 Provider ID');
      return;
    }
    if (!/^[a-z][a-z0-9_]*$/.test(formState.provider.trim())) {
      message.error('Provider ID 只能包含小写字母、数字、下划线，且以字母开头');
      return;
    }
  }
  if (!formState.name.trim()) {
    message.error('请输入显示名称');
    return;
  }

  saving.value = true;
  try {
    const payload = {
      provider: formState.provider.trim(),
      name: formState.name.trim(),
      base_url: formState.base_url,
      api_key: formState.api_key,
      chat_model: formState.chat_model,
      embedding_model: formState.embedding_model,
      rerank_model: formState.rerank_model,
    };

    if (editingProvider.value) {
      // 更新现有配置（后端 upsert，内置 provider 也能保存进数据库）
      await http.post('/system/config/providers/' + editingProvider.value, payload);
      message.success('配置已更新');
    } else {
      // 新增配置
      await http.post('/system/config/providers', payload);
      message.success('配置已创建');
    }

    showModal.value = false;
    await loadProviderConfigs();
  } catch (e: any) {
    console.error('Save provider error:', e);
    message.error(e?.detail || e?.message || '保存配置失败');
  } finally {
    saving.value = false;
  }
}

// 删除配置（表格里的 Popconfirm 已做二次确认，这里直接执行）
async function deleteProvider(record: ProviderConfig) {
  if (!canManage.value) {
    message.warning('无修改权限');
    return;
  }

  try {
    await http.delete('/system/config/providers/' + record.provider);
    message.success('删除成功');
    await loadProviderConfigs();
  } catch (e: any) {
    console.error('Delete provider error:', e);
    message.error(e?.detail || e?.message || '删除失败');
  }
}

onMounted(() => {
  loadProviderConfigs();
});
</script>

<template>
  <div class="page-card model-management-page">
    <a-alert v-if="!canManage" type="info" show-icon style="margin-bottom: 16px">
      <template #message>
        <span>您只有查看权限，如需修改请联系管理员授予 <code>system:manage</code> 权限</span>
      </template>
    </a-alert>

    <a-card title="模型管理" :bordered="false">
      <template #extra>
        <a-button @click="loadProviderConfigs" :loading="loading">
          <template #icon><ReloadOutlined /></template>
          刷新
        </a-button>
        <a-button v-if="canManage" type="primary" @click="openCreateModal" style="margin-left: 8px">
          <template #icon><PlusOutlined /></template>
          新增
        </a-button>
      </template>

      <a-table
        :data-source="providerList"
        :columns="tableColumns"
        :loading="loading"
        :pagination="false"
        row-key="provider"
        :scroll="{ x: 1200 }"
        size="middle"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'provider'">
            <a-tag>{{ record.provider }}</a-tag>
          </template>

          <template v-if="column.key === 'actions'">
            <template v-if="canManage">
              <a-space>
                <a @click="openEditModal(record as ProviderConfig)">编辑</a>
                <a-popconfirm
                  title="确定要删除该配置吗？"
                  ok-text="删除"
                  cancel-text="取消"
                  :ok-type="'danger'"
                  @confirm="deleteProvider(record as ProviderConfig)"
                >
                  <a style="color: #ff4d4f">删除</a>
                </a-popconfirm>
              </a-space>
            </template>
            <span v-else style="color: var(--app-muted)">-</span>
          </template>
        </template>
      </a-table>

      <!-- 新增/编辑对话框 -->
      <a-modal
        v-model:open="showModal"
        :title="editingProvider ? '编辑模型配置' : '新增模型配置'"
        :confirm-loading="saving"
        @ok="saveProvider"
        @cancel="showModal = false"
        width="600px"
      >
        <a-form layout="vertical">
          <a-form-item label="Provider ID" required>
            <a-input
              v-model:value="formState.provider"
              :disabled="!!editingProvider"
              placeholder="例如：custom-provider"
            />
            <template #help>
              <span style="color: #999; font-size: 12px">唯一标识符，小写字母开头，可含数字和下划线；创建后不可修改</span>
            </template>
          </a-form-item>

          <a-form-item label="显示名称" required>
            <a-input
              v-model:value="formState.name"
              placeholder="例如：自定义模型服务"
            />
          </a-form-item>

          <a-form-item label="服务地址 (Base URL)">
            <a-input
              v-model:value="formState.base_url"
              placeholder="http://localhost:8080/v1"
            />
          </a-form-item>

          <a-form-item label="API Key">
            <a-input-password
              v-model:value="formState.api_key"
              placeholder="sk-..."
            />
          </a-form-item>

          <a-form-item label="对话模型 (Chat Model)">
            <a-input
              v-model:value="formState.chat_model"
              placeholder="gpt-4o-mini"
            />
          </a-form-item>

          <a-form-item label="向量模型 (Embedding Model)">
            <a-input
              v-model:value="formState.embedding_model"
              placeholder="text-embedding-3-small"
            />
          </a-form-item>

          <a-form-item label="重排序模型 (Rerank Model)">
            <a-input
              v-model:value="formState.rerank_model"
              placeholder="例如：bge-reranker-v2-m3，可直接输入自定义名称"
            />
          </a-form-item>
        </a-form>
      </a-modal>
    </a-card>
  </div>
</template>

<style scoped>
.model-management-page {
  :deep(.ant-table-thead > tr > th) {
    background: var(--app-panel-bg, #fafafa);
  }
}
</style>
