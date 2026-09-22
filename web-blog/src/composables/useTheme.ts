/**
 * 明暗主题：单点管理 + CSS 变量（与后台同一套精神，组件里禁止硬编码明暗色）。
 *
 * 状态存 localStorage（键 blog-theme），data-theme 属性挂在 <html> 上，
 * global.css 用 [data-theme='dark'] 覆盖变量；initTheme 在挂载前调用防闪烁。
 */
import { ref } from 'vue';

const KEY = 'blog-theme';

export const isDark = ref(false);

function apply(dark: boolean): void {
  isDark.value = dark;
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
}

/** 挂载前初始化：跟随系统偏好，用户手动切换过则以存储为准 */
export function initTheme(): void {
  const stored = localStorage.getItem(KEY);
  const dark = stored ? stored === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
  apply(dark);
}

export function toggleTheme(): void {
  const next = !isDark.value;
  localStorage.setItem(KEY, next ? 'dark' : 'light');
  apply(next);
}
