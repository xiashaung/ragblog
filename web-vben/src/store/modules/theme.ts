/**
 * 主题（明 / 暗）store —— 三态：跟随系统 / 浅色 / 深色，默认跟随系统。
 *
 * 约定：
 * - 用户没显式选过时 mode = 'system'，此时读 prefers-color-scheme 并监听系统切换实时跟随；
 * - 用户的选择存 localStorage（键 admin_theme_mode），'system' 视为「未选择」故删除键，
 *   这样系统切换仍能跟随；
 * - isDark 变化时同时做三件事：给 <html> 打 data-theme / color-scheme（供 global.css 的
 *   CSS 变量切换）、更新 a-config-provider 用的 themeConfig、调 ConfigProvider.config
 *   让 Modal.confirm / message / notification 这类命令式弹窗也跟随（它们不在组件树内）。
 */
import { computed, ref, watch } from 'vue';
import { defineStore } from 'pinia';
import { ConfigProvider, theme as antdTheme } from 'ant-design-vue';

export type ThemeMode = 'system' | 'light' | 'dark';

const STORAGE_KEY = 'admin_theme_mode';

function readStoredMode(): ThemeMode {
  const raw = localStorage.getItem(STORAGE_KEY);
  return raw === 'light' || raw === 'dark' ? raw : 'system';
}

export const useThemeStore = defineStore('theme', () => {
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  const mode = ref<ThemeMode>(readStoredMode());
  const systemDark = ref(media.matches);

  // 系统偏好变化时实时跟随（仅在 mode === 'system' 时影响 isDark）
  const onSystemChange = (e: MediaQueryListEvent) => {
    systemDark.value = e.matches;
  };
  if (typeof media.addEventListener === 'function') {
    media.addEventListener('change', onSystemChange);
  }

  const isDark = computed(() =>
    mode.value === 'system' ? systemDark.value : mode.value === 'dark'
  );

  /** 给 <a-config-provider :theme> 用；组件树内的所有 antd 组件随之换算法 */
  const themeConfig = computed(() => ({
    algorithm: isDark.value ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
  }));

  function apply() {
    const root = document.documentElement;
    const dark = isDark.value;
    root.dataset.theme = dark ? 'dark' : 'light';
    // 让浏览器原生控件（滚动条、表单、日期选择）也跟着配色
    root.style.colorScheme = dark ? 'dark' : 'light';
    // 命令式 API（Modal.confirm / notification / message）在组件树外渲染，需单独同步
    ConfigProvider.config({ theme: themeConfig.value });
  }

  watch(isDark, apply, { immediate: true });

  function setMode(next: ThemeMode) {
    mode.value = next;
    if (next === 'system') localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, next);
  }

  return { mode, isDark, themeConfig, setMode };
});
