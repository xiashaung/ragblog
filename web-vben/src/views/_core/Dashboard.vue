<script setup lang="ts">
/**
 * 概览 —— 整个平台的首页看板：知识库规模、模型使用、会话与权限体量一屏看完。
 *
 * 由原「概览」与知识库「统计概览」合并而来：两页取的数据本就重叠（provider / 健康 /
 * 文档统计），分两处维护只会互相漏改。知识库分组内不再放统计页，
 * 旧的 /knowledge/overview 链接由路由重定向到这里。
 *
 * 模型相关的数字一律以数据库为准（「系统管理 → 模型选择 / 模型接入」），.env 只是兜底，
 * 所以这里看不到任何「改 .env 后重建」之类的指引 —— 那套话在合并时已经过期。
 *
 * 权限：各区块按 RBAC 权限码各自取数，无权就不发这个请求并显示「—」，
 * 不让首页因为缺一个权限码而整页报错。
 */
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { adminApi, chatApi, datasourceApi, ragApi, rbacApi, systemConfigApi } from '@/api';
import { useAuthStore } from '@/store/modules/auth';
import { useBusinessStore } from '@/store/modules/business';
import type { KnowledgeStats } from '@/typings';

const router = useRouter();
const auth = useAuthStore();
const business = useBusinessStore();

const loading = ref(false);
const stats = ref<KnowledgeStats | null>(null);
/** 各功能选定的 provider + 重排序开关（/system/config/models） */
const models = ref<Record<string, string>>({});
const chatCount = ref<number | null>(null);
const liveCount = ref<number | null>(null);
const dsCount = ref<number | null>(null);
const accountCount = ref<number | null>(null);
const roleCount = ref<number | null>(null);

/** provider 名 → 生效配置（/ai/providers），供「模型使用」表取端点与模型名 */
const providerMap = computed<Record<string, any>>(() => {
  const map: Record<string, any> = {};
  business.providers.forEach((p: any) => {
    if (p?.name) map[String(p.name).toLowerCase()] = p;
  });
  return map;
});

// 空值显示「—」而不是 0：0 会被读成「真的没有」，未授权才是没有数据
function dash(v: number | null | undefined): number | string {
  return v === null || v === undefined ? '—' : v;
}

/** 首页多处要按权限码取舍，统一走这一个入口（写权限隐含读权限，见 auth store） */
function can(code: string | string[]): boolean {
  return auth.hasPerm(code);
}

/**
 * 指标卡片：perm 是「进那个页面要的权限」，卡片能否点、脚注怎么说都由它定。
 *
 * 无权时卡片还在（数据是「—」），但点了不该落到 /403：看不进去就不给进。
 */
const metricCards = computed(() => [
  { key: 'docs', title: '知识文档', value: dash(stats.value?.docs), perm: 'knowledge:read', foot: can('knowledge:read') ? `已学习 ${stats.value?.indexed_docs ?? 0} · 待重建 ${stats.value?.pending_docs ?? 0}` : '需 knowledge:read', to: '/knowledge' },
  { key: 'chunks', title: '向量分片', value: dash(stats.value?.chunks), perm: 'knowledge:read', foot: `维度 ${stats.value?.vector_dim ?? '—'}`, to: '/knowledge' },
  { key: 'chars', title: '正文字符', value: dash(stats.value?.chars), perm: 'knowledge:read', foot: `最近更新 ${formatDate(stats.value?.latest_updated_at)}`, to: '/knowledge' },
  { key: 'chats', title: '我的对话', value: dash(chatCount.value), perm: '', foot: '仅本人可见（上限 200）', to: '/chat' },
  { key: 'sessions', title: '在线会话', value: dash(liveCount.value), perm: 'session:read', foot: can('session:read') ? '可强制下线' : '需 session:read', to: '/sessions' },
  { key: 'ds', title: '外部数据源', value: dash(dsCount.value), perm: 'datasource:read', foot: can('datasource:read') ? '供智能问数切换' : '需 datasource:read', to: '/datasources' },
  { key: 'accounts', title: '账号', value: dash(accountCount.value), perm: 'account:read', foot: can('account:read') ? '含未启用' : '需 account:read', to: '/accounts' },
  { key: 'roles', title: '角色', value: dash(roleCount.value), perm: 'role:read', foot: can('role:read') ? '' : '需 role:read', to: '/roles' },
]);

/** 四个功能各用什么模型：provider 来自系统配置，模型名来自模型接入 */
const modelRows = computed(() => {
  const m = models.value;
  const rows = [
    { key: 'chat', feature: 'AI 对话', provider: m.chat_provider },
    { key: 'rag', feature: '知识库问答', provider: m.rag_provider },
    { key: 'embedding', feature: '向量嵌入', provider: m.embedding_provider },
    // 重排序是个开关：关了就当没配，不报“待补充”
    { key: 'rerank', feature: '重排序', provider: m.rerank_enabled === 'true' ? m.rerank_provider : '' },
  ];
  return rows.map((r) => {
    const cfg = r.provider ? providerMap.value[String(r.provider).toLowerCase()] : null;
    // 重排序模型名是全局配置（后端直接给出实际调用用的那个），
    // 不随 provider 列走；其余三个功能取 provider 上的对应字段
    const model =
      r.key === 'rerank'
        ? m.rerank_model || ''
        : cfg?.[r.key === 'embedding' ? 'embedding_model' : 'chat_model'] || '';
    const keyReady = r.key === 'rerank' ? true : Boolean(cfg?.api_key_configured);
    return { ...r, model, ready: Boolean(cfg) && keyReady && Boolean(model) };
  });
});

const rerankOn = computed(() => models.value.rerank_enabled === 'true');

/** 需要人工处理的事项：从知识库统计与 provider 配置里推导
 *
 * 每条都带上 ``need``（做成这件事需要的权限码）：无权的人看不到做不到的提醒，
 * 也不会被「去处理」带到 403 页。 */
const issues = computed(() => {
  const list: {
    level: 'error' | 'warning' | 'info';
    text: string;
    to: string;
    action: string;
    need: string | string[];
  }[] = [];
  const s = stats.value;
  if (s?.vectorless_chunks) {
    list.push({
      level: 'error',
      text: `${s.vectorless_chunks} 个分片没有可用向量，检索不到（多为换了 embedding 模型、维度不符）`,
      to: '/knowledge',
      action: '去重建',
      need: 'knowledge:manage',
    });
  }
  if (s?.pending_docs) {
    list.push({
      level: 'warning',
      text: `${s.pending_docs} 篇文档待重建（尚未学习或刚切换嵌入模型），检索不会命中`,
      to: '/knowledge',
      action: '去重建',
      need: 'knowledge:manage',
    });
  }
  if (s?.failed_docs) {
    list.push({
      level: 'error',
      text: `${s.failed_docs} 篇文档重建失败，需重试或重新上传原文`,
      to: '/knowledge',
      action: '去处理',
      need: 'knowledge:manage',
    });
  }
  if (s?.missing_content_docs) {
    list.push({
      level: 'info',
      text: `${s.missing_content_docs} 篇旧文档未保存原文，只能重建索引、无法编辑正文`,
      to: '/knowledge',
      action: '查看',
      need: 'knowledge:read',
    });
  }
  modelRows.value
    .filter((r) => r.provider && !r.ready)
    .forEach((r) =>
      list.push({
        level: 'warning',
        text: r.model
          ? `「${r.feature}」选的 ${r.provider} 还没配端点凭据（API Key）`
          : `「${r.feature}」选的 ${r.provider} 在模型接入里没配${r.feature === '重排序' ? '重排序' : ''}模型名`,
        to: '/model-management',
        action: '去配置',
        need: 'system:manage',
      }),
    );
  // 只在真的取到统计且确实为空时提（无 knowledge:read 的人 s 是 null，不能误报「知识库是空的」）
  if (s && !s.docs) {
    list.push({
      level: 'info',
      text: '知识库还是空的，检索问答没有可依据的内容',
      to: '/knowledge/ingest',
      action: '去录入',
      need: 'knowledge:manage',
    });
  }
  return list.filter((it) => can(it.need));
});

function formatDate(ts?: number | null): string {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

// 文档向量化状态的中文标签与颜色（与 KnowledgeDocsPage 口径一致）
const statusMeta: Record<string, { label: string; color: string }> = {
  indexed: { label: '已学习', color: 'green' },
  pending: { label: '待重建', color: 'default' },
  processing: { label: '处理中', color: 'blue' },
  failed: { label: '重建失败', color: 'red' },
};
function statusLabel(s?: string): string {
  return statusMeta[s || '']?.label ?? s ?? '—';
}
function statusColor(s?: string): string {
  return statusMeta[s || '']?.color ?? 'default';
}

// a-badge 的 status 只认 success/processing/default/error/warning，
// 写错不报错只是小点不变色，所以在这里集中映射一次
function badgeStatus(level: string): string {
  return level === 'info' ? 'default' : level;
}

function go(path: string) {
  router.push(path);
}

/** 卡片点击：缺权限就不跳（落到 /403 比不给点更让人困惑） */
function goCard(card: { perm: string; to: string }) {
  if (card.perm && !can(card.perm)) return;
  router.push(card.to);
}

// 逐块独立取数：任何一块失败（离线 / 403）只影响它自己那格显示「—」
async function load<T>(run: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await run();
  } catch {
    return fallback;
  }
}

async function refresh() {
  loading.value = true;
  await Promise.all([
    business.loadProviders(),
    business.pollHealth(),
    // 知识库与模型配置都已按权限码拦截，没权限就连请求都不发（否则只拿到一个 403）
    can('knowledge:read')
      ? load(() => ragApi.stats().then((s) => (stats.value = s)), null as KnowledgeStats | null)
      : Promise.resolve(),
    can('system:read')
      ? load(() => systemConfigApi.models().then((v) => (models.value = v)), {})
      : Promise.resolve(),
    load(() => chatApi.sessions(200).then((r) => (chatCount.value = r.sessions?.length ?? 0)), null),
    can('session:read')
      ? load(() => adminApi.sessions().then((r) => (liveCount.value = r?.length ?? 0)), null)
      : Promise.resolve(),
    can('datasource:read')
      ? load(() => datasourceApi.list().then((r) => (dsCount.value = r?.length ?? 0)), null)
      : Promise.resolve(),
    can('account:read')
      ? load(() => rbacApi.accounts().then((r) => (accountCount.value = r?.length ?? 0)), null)
      : Promise.resolve(),
    can('role:read')
      ? load(() => rbacApi.roles().then((r) => (roleCount.value = r?.length ?? 0)), null)
      : Promise.resolve(),
  ]);
  loading.value = false;
}

onMounted(refresh);
</script>

<template>
  <div class="page-card">
    <div class="head">
      <div>
        <h2 class="title">概览</h2>
        <p class="sub">
          知识库 / 模型 / 账号权限的整体情况，数据实时取自后端
        </p>
      </div>
      <a-button :loading="loading" @click="refresh">刷新</a-button>
    </div>

    <a-alert
      v-if="business.backendOk === false"
      type="warning"
      show-icon
      banner
      message="后端 /health 未响应，下方统计可能不完整"
    />

    <a-spin :spinning="loading">
      <!-- 核心指标 -->
      <a-row :gutter="[16, 16]">
        <a-col v-for="c in metricCards" :key="c.key" :xs="12" :md="6">
          <a-card :bordered="false" size="small" :hoverable="!c.perm || can(c.perm)" @click="goCard(c)">
            <a-statistic :title="c.title" :value="c.value" />
            <div class="sub">{{ c.foot }}</div>
          </a-card>
        </a-col>
      </a-row>

      <!-- 模型使用 + 待处理事项 -->
      <a-row :gutter="[16, 16]" style="margin-top: 16px">
        <a-col :xs="24" :md="14">
          <a-card :bordered="false" title="模型使用" size="small">
            <template #extra>
              <a-space>
                <a-button v-perm="'system:read'" type="link" size="small" @click="go('/system-config')">模型选择</a-button>
                <a-button v-perm="'system:read'" type="link" size="small" @click="go('/model-management')">模型接入</a-button>
              </a-space>
            </template>
            <a-table
              :data-source="modelRows"
              :columns="[
                { title: '功能', dataIndex: 'feature', key: 'feature', width: 110 },
                { title: 'Provider', dataIndex: 'provider', key: 'provider', width: 120 },
                { title: '模型名', dataIndex: 'model', key: 'model', ellipsis: true },
                { title: '状态', key: 'state', width: 96 },
              ]"
              :pagination="false"
              :row-key="(r: any) => r.key"
              size="small"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'provider'">
                  <a-tag v-if="record.provider" color="blue">{{ record.provider }}</a-tag>
                  <span v-else class="muted">未启用</span>
                </template>
                <template v-else-if="column.key === 'model'">
                  <span :class="{ muted: !record.model }">{{ record.model || '—' }}</span>
                </template>
                <template v-else-if="column.key === 'state'">
                  <a-tooltip
                    :title="
                      !record.provider
                        ? '该功能未启用重排序'
                        : record.ready
                          ? '端点与模型名均已配置'
                          : record.model
                            ? '缺少端点凭据（API Key）'
                            : '该 provider 还没配这个模型名'
                    "
                  >
                    <a-tag :color="!record.provider ? 'default' : record.ready ? 'green' : 'orange'">
                      {{ !record.provider ? '未启用' : record.ready ? '就绪' : '待补充' }}
                    </a-tag>
                  </a-tooltip>
                </template>
              </template>
            </a-table>
            <div class="sub">
              <template v-if="can('system:read')">
                这里的值取自数据库（{{ rerankOn ? '重排序已开启' : '重排序已关闭' }}），
                改完即时生效、无需重启后端
              </template>
              <template v-else>
                没有 system:read，未取模型配置（下面全是「未启用」并不等于真没配）
              </template>
            </div>
          </a-card>
        </a-col>

        <a-col :xs="24" :md="10">
          <a-card :bordered="false" title="待处理事项" size="small">
            <a-empty v-if="!issues.length" description="一切正常，没有需要处理的事项" />
            <div v-else class="issues">
              <div v-for="(it, i) in issues" :key="i" class="issue">
                <a-badge :status="badgeStatus(it.level)" />
                <span class="issue-text">{{ it.text }}</span>
                <a-button class="issue-action" type="link" size="small" @click="go(it.to)">
                  {{ it.action }}
                </a-button>
              </div>
            </div>
          </a-card>
        </a-col>
      </a-row>

      <!-- provider 分布 + 向量存储 / 快捷入口 -->
      <a-row :gutter="[16, 16]" style="margin-top: 16px">
        <a-col :xs="24" :md="14">
          <a-card :bordered="false" title="知识库 · 按状态分布" size="small">
            <a-table
              :data-source="stats?.by_status || []"
              :columns="[
                { title: '状态', dataIndex: 'status', key: 'status' },
                { title: '文档', dataIndex: 'docs', key: 'docs', width: 80 },
                { title: '分片', dataIndex: 'chunks', key: 'chunks', width: 80 },
              ]"
              :pagination="false"
              :row-key="(r: any) => r.status"
              size="small"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'status'">
                  <a-tag :color="statusColor(record.status)">{{ statusLabel(record.status) }}</a-tag>
                </template>
              </template>
            </a-table>
          </a-card>
        </a-col>

        <a-col :xs="24" :md="10">
          <a-card :bordered="false" title="向量存储与检索" size="small">
            <a-descriptions :column="1" size="small">
              <a-descriptions-item label="向量后端">
                <a-tooltip title="pgvector：分片向量存进 vector 列，相似度由 Postgres 计算并走 HNSW 索引">
                  <a-tag :color="stats?.vector_backend === 'pgvector' ? 'geekblue' : 'default'">
                    {{ stats?.vector_backend === 'pgvector' ? 'pgvector · HNSW' : 'JSON（SQLite 回退）' }}
                  </a-tag>
                </a-tooltip>
              </a-descriptions-item>
              <a-descriptions-item label="向量模型">
                {{ stats?.embedding_model || '—' }}
                <span class="muted">（{{ stats?.vector_dim ?? '—' }} 维）</span>
              </a-descriptions-item>
              <a-descriptions-item label="最近更新">
                {{ formatDate(stats?.latest_updated_at) }}
              </a-descriptions-item>
            </a-descriptions>
            <a-divider style="margin: 8px 0" />
            <a-space wrap>
              <a-button v-perm="'knowledge:read'" size="small" @click="go('/knowledge')">文档列表</a-button>
              <a-button v-perm="'knowledge:manage'" size="small" type="primary" @click="go('/knowledge/ingest')">录入内容</a-button>
              <a-button v-perm="'knowledge:read'" size="small" @click="go('/knowledge/search')">检索问答</a-button>
              <a-button v-perm="'analytics:read'" size="small" @click="go('/analytics')">智能问数</a-button>
            </a-space>
          </a-card>
        </a-col>
      </a-row>
    </a-spin>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
  gap: 16px;
}

.title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: var(--app-fg);
}

.sub {
  font-size: 12px;
  color: var(--app-muted);
  margin: 2px 0 0;
}

.muted {
  color: var(--app-muted);
}

.issues {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.issue {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
  line-height: 20px;
  color: var(--app-fg);
}

.issue-text {
  flex: 1;
}

.issue-action {
  flex: none;
}
</style>
