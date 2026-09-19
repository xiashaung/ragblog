/**
 * 业务 Store —— provider 元数据 / 后端健康状态 / admin 会话列表。
 */
import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import { adminApi, aiApi } from '@/api';
import type { SessionItem } from '@/typings';

export const useBusinessStore = defineStore('business', () => {
  const providers = ref<any[]>([]);
  const backendOk = ref<boolean | null>(null);
  const sessions = ref<SessionItem[]>([]);

  // 当前生效的后端配置：/ai/providers 已将 llm_default_provider 排在首位，
  // 前端不再选择平台，只用它做只读展示。
  const activeProvider = computed<any>(() => providers.value[0] ?? null);

  async function loadProviders() {
    try {
      providers.value = await aiApi.providers();
    } catch {
      /* 后端离线由 banner 覆盖 */
    }
  }

  async function pollHealth() {
    try {
      const data = await aiApi.health();
      backendOk.value = data?.status === 'ok';
    } catch {
      backendOk.value = false;
    }
  }

  async function loadSessions() {
    // 需要 session:read；权限不足时静默为空（菜单也不会展示这一页）
    try {
      sessions.value = await adminApi.sessions();
    } catch {
      sessions.value = [];
    }
  }

  return { providers, activeProvider, backendOk, sessions, loadProviders, pollHealth, loadSessions };
});
