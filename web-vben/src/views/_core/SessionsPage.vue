<script setup lang="ts">
/**
 * 会话页 —— 在线后台会话（token 存的是 SHA-256 摘要，这里只能看到归属与过期时间）。
 * 读需 session:read，强制下线需 session:revoke（按钮无权时置灰 + 悬停说明）。
 */
import { computed, onMounted, ref } from 'vue';
import { Alert, Button, Card, Popconfirm, Space, Table, Tag, message } from 'ant-design-vue';
import { adminApi } from '@/api';
import { usePerm } from '@/composables/usePerm';
import { useAuthStore } from '@/store/modules/auth';
import { useBusinessStore } from '@/store/modules/business';
import type { SessionItem } from '@/typings';

const auth = useAuthStore();
const business = useBusinessStore();
const loading = ref(false);

const { can } = usePerm();
const canRevoke = computed(() => can('session:revoke'));
const isSelf = (row: SessionItem) => row.admin_id === auth.userInfo?.id;

const columns = [
  { title: '会话 ID', dataIndex: 'id', key: 'id', ellipsis: true },
  { title: '账号', key: 'username' },
  { title: '过期时间', key: 'expires_at', width: 200 },
  { title: '操作', key: 'actions', width: 140 },
];

async function refresh() {
  loading.value = true;
  try {
    await business.loadSessions();
  } finally {
    loading.value = false;
  }
}

async function onRevoke(row: SessionItem) {
  try {
    await adminApi.revokeSession(row.id);
    message.success('该会话已强制下线');
    await refresh();
  } catch (e) {
    const err = e as { detail?: string; message?: string };
    message.error(err?.detail || err?.message || '下线失败');
  }
}

onMounted(refresh);
</script>

<template>
  <div class="page-card">
    <Card :bordered="false" title="在线会话">
      <template #extra>
        <Space>
          <Tag color="blue">{{ business.sessions.length }} 个</Tag>
          <Button @click="refresh">刷新</Button>
        </Space>
      </template>
      <Alert
        v-if="!canRevoke"
        type="info"
        show-icon
        style="margin-bottom: 12px"
        message="当前账号缺少 session:revoke，只能查看，不能强制下线"
      />
      <Table
        :data-source="business.sessions"
        :columns="columns"
        :loading="loading"
        row-key="id"
        size="middle"
        :pagination="{ pageSize: 15, hideOnSinglePage: true }"
      >
        <!-- ant-design-vue 4 只支持 #bodyCell 这一个单元格插槽，按 column.key 分支渲染 -->
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'username'">
            <Space>
              {{ record.username || '未知' }}
              <Tag v-if="isSelf(record)" color="green">当前会话</Tag>
            </Space>
          </template>
          <template v-else-if="column.key === 'expires_at'">
            {{ new Date(record.expires_at * 1000).toLocaleString() }}
          </template>
          <template v-else-if="column.key === 'actions'">
            <Popconfirm
              title="强制下线后该 token 立即失效，需要重新登录"
              :disabled="!canRevoke"
              @confirm="onRevoke(record)"
            >
              <Button size="small" danger v-perm="'session:revoke'">下线</Button>
            </Popconfirm>
          </template>
        </template>
      </Table>
    </Card>
  </div>
</template>
