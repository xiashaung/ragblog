<script setup lang="ts">
/**
 * 嵌入向量页 —— 批量对若干文本调用 /ai/embeddings（非流式）。
 * provider / model 由后端 .env 配置决定，前端不再选择平台。
 */
import { computed, onMounted, ref } from 'vue';
import { Button, Input, message, Card, Result, Table } from 'ant-design-vue';
import { embedApi } from '@/api';
import { useBusinessStore } from '@/store/modules/business';

const business = useBusinessStore();
const backend = computed<any>(() => business.activeProvider);

const texts = ref('');
const loading = ref(false);
const rows = ref<any[]>([]);
const errorMsg = ref('');

async function onRun() {
  const list = texts.value
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean);
  if (!list.length) return message.warning('请输入文本（每行一条）');
  loading.value = true;
  errorMsg.value = '';
  rows.value = [];
  try {
    const res = await embedApi.run({ input: list });
    const data = ((res as any)?.data ?? res) as any;
    const items: any[] = data?.items || [];
    rows.value = list.map((input: string, i: number) => ({
      input,
      vec: (items[i]?.embedding || [])
        .slice(0, 6)
        .map((v: number) => v.toFixed(3))
        .join(', '),
    }));
    if (!items.length) message.warning('未获取到向量（检查后端 embedding 配置）');
    else message.success(`成功嵌入 ${items.length} 条（模型 ${data?.model || '—'}）`);
  } catch (e: any) {
    errorMsg.value = (e as any).detail || (e as any).message || '嵌入请求失败';
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  business.loadProviders();
  business.pollHealth();
});
</script>

<template>
  <div class="page-card">
    <a-alert v-if="!business.backendOk" type="warning" :message="`后端当前未响应（/health），嵌入可能失败`" />
    <a-alert
      type="info"
      show-icon
      style="margin-top: 8px"
      :message="`Embedding 配置来自后端：${backend?.name || '—'} · ${backend?.embedding_model || '默认模型'}`"
    />

    <a-row :gutter="16">
      <a-col :span="12">
        <Card title=" 文本嵌入" :bordered="false">
          <a-form layout="vertical">
            <a-form-item label="待嵌入文本（每行一条）">
              <Input.Textarea
                v-model:value="texts"
                :rows="8"
                placeholder="逐行输入"
              />
            </a-form-item>
            <Button type="primary" :loading="loading" @click="onRun">开始嵌入</Button>
          </a-form>
        </Card>
      </a-col>

      <a-col :span="12">
        <Card title=" 嵌入结果" :bordered="false">
          <a-alert v-if="errorMsg" type="error" :message="errorMsg" style="margin-bottom:12px" />
          <template v-if="rows.length">
            <Table
              :dataSource="rows"
              :columns="[
                { title: '原文', dataIndex: 'input', key: 'input' },
                { title: '向量(前6维)', dataIndex: 'vec', key: 'vec' },
              ]"
              :pagination="false"
              row-key="(row: any, index: number) => index"
              size="small"
            >
            </Table>
          </template>
          <template #empty><Result status="info" title="尚未嵌入" /></template>
        </Card>
      </a-col>
    </a-row>
  </div>
</template>
