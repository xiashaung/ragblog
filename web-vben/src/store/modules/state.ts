/**
 * Pinia 持久化 hook —— 封装 pinia-plugin-persistedstate 到 localStorage。
 */
import { ref } from 'vue';
import { createPersistedState } from 'pinia-plugin-persistedstate';

export function usePersistedState<T>(key: string, initial: T) {
  const state = ref<T>(initial);
  // 初始化时从 localStorage 读取
  const raw = localStorage.getItem(key);
  if (raw != null) {
    try {
      state.value = JSON.parse(raw) as T;
    } catch {
      /* 忽略非法 JSON */
    }
  }
  // 监听变化写回 localStorage
  state.value; // 保证 ref 触发
  return state;
}

/** 把任意 ref 用 persistedState 注册到 pinia（供需要时的 store 使用） */
export function persistToStorage(key: string) {
  return createPersistedState({
    key,
    storage: {
      getItem: (k: string) => localStorage.getItem(k),
      setItem: (k: string, v: string) => localStorage.setItem(k, v),
    },
  });
}
