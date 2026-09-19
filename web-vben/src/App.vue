<script setup lang="ts">
/** 根组件：负责登录态校验 + 重定向，并作为全局主题（明/暗）的注入点。
 * 布局由路由配置（MainLayout 作为 / 的 component）统一管理，App.vue 只负责把 router-view 丢出去。
 */
import { computed, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useAuthStore } from '@/store/modules/auth';
import { useThemeStore } from '@/store/modules/theme';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
// 主题 store 一被实例化就会立即 apply()（写 <html data-theme> + 同步命令式弹窗主题）
const theme = useThemeStore();

const token = localStorage.getItem('admin_token');
const isAuthenticated = !!token;
const isAuthPage = computed(() => route.path.startsWith('/auth'));

function redirectIfUnauthed() {
  if (!isAuthenticated && !isAuthPage.value && route.path !== '/auth/login') {
    router.replace('/auth/login');
  }
}
onMounted(redirectIfUnauthed);

function ensureLogin() {
  localStorage.removeItem('admin_token');
  localStorage.removeItem('user_info');
  router.push('/auth/login');
}
</script>

<template>
  <!-- a-config-provider 换 algorithm 即可整体切换 antd 明暗；登录页也在树内，同样生效 -->
  <a-config-provider :theme="theme.themeConfig">
    <!-- 未登录时，LoginPage 通过路由 /auth/login 挂载，router-view 会自动渲染它 -->
    <router-view />
  </a-config-provider>
</template>
