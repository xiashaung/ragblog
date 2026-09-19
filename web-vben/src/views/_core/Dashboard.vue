<script setup lang="ts">
/**
 * 概览页 —— 展示后端 provider 配置 / 健康 / 在线会话 / 当前 Key（纯后端数据驱动）。
 */
import { computed, onMounted } from 'vue';
import { Card, Statistic, Tag, Alert, Table, Descriptions, Button } from 'ant-design-vue';
import { useBusinessStore } from '@/store/modules/business';
import { useAuthStore } from '@/store/modules/auth';

const business = useBusinessStore();
const auth = useAuthStore();

// 后端 provider 字段：{name, base_url, chat_model, embedding_model, api_key_configured}
const activeProviders = computed(() =>
  business.providers.filter((p: any) => p.api_key_configured).map((p: any) => p.name),
);

async function refresh() {
  await Promise.all([
    business.loadProviders(),
    business.pollHealth(),
    business.loadSessions(),
  ]);
}

onMounted(refresh);
</script>

<template>
  <div class="page-card">
    <a-row :gutter="16">
      <a-col :span="6">
        <Card title=" 后端状态" :bordered="false">
          <Statistic
            title=" 是否在线"
            :value="business.backendOk === null ? '-' : business.backendOk ? '在线' : '离线'"
            :value-style="{ color: business.backendOk === true ? '#16a34a' : '#dc2626' }"
          />
          <div class="mt-2"><Button @click="refresh">刷新</Button></div>
        </Card>
      </a-col>
      <a-col :span="6">
        <Card title=" 已启用 Provider" :bordered="false">
          <Statistic title=" 已启用 Provider" :value="activeProviders.length" />
          <div class="tags">{{ activeProviders.map((n: string) => n).join(', ') || '—' }}</div>
        </Card>
      </a-col>
      <a-col :span="6">
        <Card title=" 在线会话数" :bordered="false">
          <Statistic title=" 在线会话数" :value="business.sessions.length" />
        </Card>
      </a-col>
    </a-row>

    <a-col :span="24">
      <Card title=" 后端 Provider 详情" :bordered="false" style="margin-top: 16px">
        <Table :dataSource="business.providers" :columns="[
          { title: '名称', dataIndex: 'name', key: 'name' },
          { title: 'Base URL', dataIndex: 'base_url', key: 'base_url' },
          { title: '模型', dataIndex: 'chat_model', key: 'chat_model' },
          { title: '状态', key: 'configured' }
        ]" row-key="(row: any, index: number) => index.key || index">
          <template #bodyCell="{ column, record }">
            <Tag v-if="column.key === 'configured'" :color="record.api_key_configured ? 'green' : 'default'">{{ record.api_key_configured ? '已配置' : '未配置' }}</Tag>
          </template>
        </Table>
      </Card>
    </a-col>
  </div>
</template>

<style scoped>
.tags { font-size: 12px; color: var(--app-muted); margin-top: 6px; }
.mt-2 { margin-top: 8px; }
</style>
