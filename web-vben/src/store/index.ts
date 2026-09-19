/**
 * Pinia 初始化 —— 注册所有 store。
 * auth store 手动读写 localStorage（token/userInfo），其余 store 手动调用。
 */
import { createPinia } from 'pinia';

export const pinia = createPinia();

export { useAuthStore } from './modules/auth';
export { useBusinessStore } from './modules/business';
export { useThemeStore } from './modules/theme';
