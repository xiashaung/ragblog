<script setup lang="ts">
/**
 * 模型选择 —— 为各功能模块选择模型提供者。
 *
 * 权限：读需 system:read（路由已挡），写需 system:manage。本页没有「保存按钮」，
 * 下拉/开关改动即提交，所以按钮级门控落在控件本身：无权时保持禁用，并把缺的权限码悬停出来。
 * 下拉选项统一从「模型接入」（provider_configs 表 + .env 内置兜底）动态加载，
 * 只能选择在模型接入中已配置的 provider。
 * 配置项：
 * - chat_provider / rag_provider / embedding_provider / rerank_provider：各功能的 provider 选择
 * - rerank_enabled：是否启用重排序服务
 */
import { computed, onMounted, ref } from 'vue';
import { message } from 'ant-design-vue';
import { ReloadOutlined } from '@ant-design/icons-vue';
import http from '@/api/request';
import { usePerm } from '@/composables/usePerm';

const { can, tip: permHint, need } = usePerm();
const canManage = computed(() => can('system:manage'));
// 无权时给控件挂的悬停文案，有权时为空（a-tooltip 空标题不渲染）
const lockedTip = computed(() => (canManage.value ? '' : permHint('system:manage')));

const loading = ref(false);
const saving = ref(false);

// 从模型接入动态加载可用的 provider 选项
const providerOptions = ref<{ label: string; value: string }[]>([]);

async function loadProviderOptions() {
  try {
    const res: any = await http.get('/system/config/providers');
    const providers = res.data ?? {};
    providerOptions.value = Object.entries(providers).map(([key, cfg]: [string, any]) => ({
      label: cfg?.name ?? key,
      value: key,
    }));
  } catch (e: any) {
    console.error('加载 provider 选项失败:', e);
    providerOptions.value = [];
  }
}

// 配置项的当前值（key -> value），由后端加载
const chatProvider = ref('');
const ragProvider = ref('');
const embeddingProvider = ref('');
const rerankEnabled = ref(false);
const rerankProvider = ref('');

// 加载全部配置：后端返回 {configs: [{key, value, description}, ...]}
async function loadConfigs() {
  loading.value = true;
  try {
    const res: any = await http.get('/system/config/');
    const list: any[] = res.data?.configs ?? [];
    const configs: Record<string, string> = {};
    list.forEach((it) => { configs[it.key] = it.value; });

    chatProvider.value = configs.chat_provider ?? '';
    ragProvider.value = configs.rag_provider ?? '';
    embeddingProvider.value = configs.embedding_provider ?? '';
    rerankEnabled.value = configs.rerank_enabled === 'true';
    rerankProvider.value = configs.rerank_provider ?? '';

    // 同步刷新可选项（模型接入里新增/删除后立即可见）
    await loadProviderOptions();
  } catch (e: any) {
    console.error('Load configs error:', e);
    message.error(e?.detail || e?.message || '加载配置失败');
  } finally {
    loading.value = false;
  }
}

// 单项更新（选择即保存）
async function updateConfig(key: string, value: string) {
  if (!canManage.value) {
    message.warning(need('system:manage'));
    return;
  }

  saving.value = true;
  try {
    if (key === 'chat_provider') {
      await http.patch('/system/config/chat-provider', { value });
    } else if (key === 'rag_provider') {
      await http.patch('/system/config/rag-provider', { value });
    } else if (key === 'rerank_enabled') {
      await http.patch('/system/config/rerank-enabled', { value });
    } else if (key === 'embedding_provider') {
      await http.patch('/system/config/embedding_provider', { value });
    } else if (key === 'rerank_provider') {
      await http.patch('/system/config/rerank-provider', { value });
    }
    message.success('配置已更新');
  } catch (e: any) {
    console.error('Update config error:', e);
    message.error(e?.detail || e?.message || `更新 ${key} 失败`);
    // 失败时回读，保持界面与服务端一致
    await loadConfigs();
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  loadConfigs();
});
</script>

<template>
  <div class="page-card system-config-page">
    <a-alert v-if="!canManage" type="info" show-icon style="margin-bottom: 16px">
      <template #message>
        <span>您只有查看权限，如需修改请联系管理员授予 <code>system:manage</code> 权限</span>
      </template>
    </a-alert>

    <a-card title="模型选择" :bordered="false">
      <template #extra>
        <a-button @click="loadConfigs" :loading="loading">
          <template #icon><ReloadOutlined /></template>
          刷新
        </a-button>
      </template>

      <a-alert type="info" show-icon style="margin-bottom: 16px">
        <template #message>
          <span>
            下拉选项来自「模型接入」中已配置的提供者，如需新增或修改模型地址、Key、模型名称，请先前往
            <router-link to="/model-management">模型接入</router-link>。
          </span>
        </template>
      </a-alert>

      <div class="config-category">
        <h3 class="category-title">LLM 模型选择</h3>

        <div class="config-item">
          <div class="config-header">
            <div class="config-info">
              <span class="config-name">AI 对话模型</span>
              <span class="config-desc">用于 AI 对话功能的 LLM 提供者</span>
            </div>
            <a-tooltip :title="lockedTip">
              <a-select
                v-model:value="chatProvider"
                :options="providerOptions"
                :disabled="!canManage"
                :loading="saving"
                placeholder="请选择已配置的模型"
                style="width: 240px"
                @change="(v: string) => updateConfig('chat_provider', v)"
              />
            </a-tooltip>
          </div>
        </div>

        <div class="config-item">
          <div class="config-header">
            <div class="config-info">
              <span class="config-name">知识库问答模型</span>
              <span class="config-desc">用于知识库问答 (RAG) 的 LLM 提供者，可与对话模型独立</span>
            </div>
            <a-tooltip :title="lockedTip">
              <a-select
                v-model:value="ragProvider"
                :options="providerOptions"
                :disabled="!canManage"
                :loading="saving"
                placeholder="请选择已配置的模型"
                style="width: 240px"
                @change="(v: string) => updateConfig('rag_provider', v)"
              />
            </a-tooltip>
          </div>
        </div>

        <div class="config-item">
          <div class="config-header">
            <div class="config-info">
              <span class="config-name">向量嵌入模型</span>
              <span class="config-desc">用于知识库文档向量化</span>
            </div>
            <a-tooltip :title="lockedTip">
              <a-select
                v-model:value="embeddingProvider"
                :options="providerOptions"
                :disabled="!canManage"
                :loading="saving"
                placeholder="请选择已配置的模型"
                style="width: 240px"
                @change="(v: string) => updateConfig('embedding_provider', v)"
              />
            </a-tooltip>
          </div>
        </div>
      </div>

      <div class="config-category">
        <h3 class="category-title">重排序配置</h3>

        <div class="config-item">
          <div class="config-header">
            <div class="config-info">
              <span class="config-name">启用重排序</span>
              <span class="config-desc">是否启用检索结果重排序服务（提升相关性）</span>
            </div>
            <a-tooltip :title="lockedTip">
              <a-switch
                v-model:checked="rerankEnabled"
                :disabled="!canManage"
                :loading="saving"
                @change="(v: boolean) => updateConfig('rerank_enabled', String(v))"
              />
            </a-tooltip>
          </div>
        </div>

        <div class="config-item">
          <div class="config-header">
            <div class="config-info">
              <span class="config-name">重排序模型</span>
              <span class="config-desc">选择提供重排序服务的模型提供者</span>
            </div>
            <a-tooltip :title="lockedTip">
              <a-select
                v-model:value="rerankProvider"
                :options="providerOptions"
                :disabled="!canManage"
                :loading="saving"
                placeholder="请选择已配置的模型"
                style="width: 240px"
                @change="(v: string) => updateConfig('rerank_provider', v)"
              />
            </a-tooltip>
          </div>
        </div>
      </div>

      <div class="tips">
        <h4>💡 使用提示：</h4>
        <ul>
          <li><strong>先接入后选择</strong>：本页下拉只展示「模型接入」中已配置的 provider</li>
          <li><strong>对话/问答模型</strong>：可分别选择不同 provider，互不影响</li>
          <li><strong>向量模型</strong>：用于文档向量化，本地部署推荐 ollama 或 mlx</li>
          <li><strong>重排序模型</strong>：启用后检索结果会经所选 provider 精排，提升相关性</li>
        </ul>
      </div>
    </a-card>
  </div>
</template>

<style scoped>
.system-config-page {
  .config-category {
    margin-bottom: 32px;
  }

  .category-title {
    font-size: 16px;
    font-weight: 600;
    color: var(--app-fg);
    margin-bottom: 16px;
    padding-left: 12px;
    border-left: 4px solid #1677ff;
  }

  .config-item {
    background: var(--app-panel-2);
    border: 1px solid var(--app-border);
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 12px;
    transition: all 0.3s;
  }

  .config-item:hover {
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  }

  .config-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
  }

  .config-info {
    flex: 1;
    min-width: 0;
  }

  .config-name {
    display: block;
    font-size: 14px;
    font-weight: 600;
    color: var(--app-fg);
    margin-bottom: 4px;
  }

  .config-desc {
    display: block;
    font-size: 12px;
    color: var(--app-muted);
  }

  .tips {
    background: #e6f7ff;
    border: 1px solid #91d5ff;
    border-radius: 8px;
    padding: 16px;
  }

  .tips h4 {
    font-size: 14px;
    font-weight: 600;
    color: #0958d9;
    margin: 0 0 12px 0;
  }

  .tips ul {
    margin: 0;
    padding-left: 20px;
    color: var(--app-muted);
    font-size: 13px;
  }

  .tips li {
    margin-bottom: 8px;
  }
}
</style>
