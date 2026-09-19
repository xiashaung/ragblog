<script setup lang="ts">
/**
 * 知识库 · 概览 —— 汇总统计（文档 / 分片 / 字符 / 最近更新）+ 当前后端配置 +
 * 各 provider 入库分布，并提供到「文档列表 / 内容录入 / 检索问答」的快捷入口。
 * 数据全部来自后端 /ai/knowledge/stats，向量化模型由 .env 决定，前端不选择平台。
 */
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
// 模板里用的是 PascalCase <Card>，全局注册只提供 a-*，必须显式引入
import { Card } from 'ant-design-vue';
import { ragApi } from '@/api';
import { useBusinessStore } from '@/store/modules/business';
import type { KnowledgeStats } from '@/typings';

const router = useRouter();
const business = useBusinessStore();
const backend = computed<any>(() => business.activeProvider);

const stats = ref<KnowledgeStats | null>(null);
const loading = ref(false);

function fmtChars(n: number): string {
  return (n || 0).toLocaleString();
}

function formatDate(ts: number | null): string {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

async function loadStats() {
  loading.value = true;
  try {
    stats.value = await ragApi.stats();
  } catch {
    stats.value = null;
  } finally {
    loading.value = false;
  }
}

function go(path: string) {
  router.push(path);
}

onMounted(() => {
  business.loadProviders();
  business.pollHealth();
  loadStats();
});
</script>

<template>
  <div class="page-card">
    <a-alert v-if="!business.backendOk" type="warning" show-icon style="margin-bottom: 12px"
      message="后端当前未响应（/health 未通过），统计数据可能不准确" />

    <a-spin :spinning="loading">
      <!-- 核心指标 -->
      <a-row :gutter="16">
        <a-col :span="6">
          <Card :bordered="false" size="small">
            <a-statistic title="文档数" :value="stats?.docs ?? 0" />
          </Card>
        </a-col>
        <a-col :span="6">
          <Card :bordered="false" size="small">
            <a-statistic title="分片数" :value="stats?.chunks ?? 0" />
          </Card>
        </a-col>
        <a-col :span="6">
          <Card :bordered="false" size="small">
            <a-statistic title="正文字符" :value="fmtChars(stats?.chars ?? 0)" />
            <div class="sub">最近更新：{{ formatDate(stats?.latest_updated_at ?? null) }}</div>
          </Card>
        </a-col>
        <a-col :span="6">
          <Card :bordered="false" size="small" title="当前后端配置">
            <a-space wrap>
              <a-tag color="blue">{{ stats?.default_provider || backend?.name || '—' }}</a-tag>
              <a-tag>embedding：{{ stats?.embedding_model || backend?.embedding_model || '默认' }}</a-tag>
              <a-tooltip title="pgvector：分片向量存进 vector 列，相似度由 Postgres 计算并走 HNSW 索引">
                <a-tag :color="stats?.vector_backend === 'pgvector' ? 'geekblue' : 'default'">
                  {{
                    stats?.vector_backend === 'pgvector'
                      ? `pgvector ${stats?.vector_dim} 维 · HNSW`
                      : `JSON 向量 ${stats?.vector_dim ?? 0} 维`
                  }}
                </a-tag>
              </a-tooltip>
              <a-tooltip v-if="stats?.vectorless_chunks" title="这些分片没有可用向量（迁移时维度不符等），检索不到；到「文档列表」对相关文档执行「重建」">
                <a-tag color="red">{{ stats?.vectorless_chunks }} 个分片无向量</a-tag>
              </a-tooltip>
              <a-tag v-if="stats?.stale_docs" color="orange">{{ stats.stale_docs }} 篇需重建索引</a-tag>
              <a-tag v-if="stats?.missing_content_docs" color="default">
                {{ stats.missing_content_docs }} 篇无原文（旧数据）
              </a-tag>
            </a-space>
            <div class="sub">
              切换平台改 APP_LLM_DEFAULT_PROVIDER 后点「重建」；换 embedding 模型（维度变化）还要同步改
              APP_RAG_VECTOR_DIM 并重跑 alembic upgrade head
            </div>
          </Card>
        </a-col>
      </a-row>

      <!-- provider 分布 + 快捷入口 -->
      <a-row :gutter="16" style="margin-top: 16px">
        <a-col :span="14">
          <Card :bordered="false" title="按 provider 分布">
            <a-table
              :data-source="stats?.by_provider || []"
              :columns="[
                { title: 'Provider', dataIndex: 'provider', key: 'provider' },
                { title: '文档', dataIndex: 'docs', key: 'docs', width: 90 },
                { title: '分片', dataIndex: 'chunks', key: 'chunks', width: 90 },
                { title: '索引状态', dataIndex: 'stale', key: 'stale', width: 110 },
              ]"
              :pagination="false"
              :row-key="(r: any) => r.provider"
              size="small"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'stale'">
                  <a-tag :color="record.stale ? 'orange' : 'green'">{{ record.stale ? '有需重建' : '最新' }}</a-tag>
                </template>
              </template>
            </a-table>
          </Card>
        </a-col>
        <a-col :span="10">
          <Card :bordered="false" title="快捷入口">
            <a-space direction="vertical" style="width: 100%">
              <a-button block @click="go('/knowledge')">查看文档列表</a-button>
              <a-button block type="primary" @click="go('/knowledge/ingest')">录入新知识</a-button>
              <a-button block @click="go('/knowledge/search')">检索与问答</a-button>
            </a-space>
          </Card>
        </a-col>
      </a-row>
    </a-spin>
  </div>
</template>

<style scoped>
.sub {
  font-size: 12px;
  color: var(--app-muted);
  margin-top: 2px;
}
</style>
