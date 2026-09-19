<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { useAuthStore } from '@/store/modules/auth';
import { Alert, Button, Input } from 'ant-design-vue';
import ThemeToggle from '@/components/ThemeToggle.vue';

const auth = useAuthStore();
const router = useRouter();
const form = ref({ username: '', password: '', error: '' });
const loading = ref(false);

async function doSubmit() {
  if (!form.value.username.trim() || !form.value.password) {
    form.value.error = '请输入用户名和密码';
    return;
  }
  loading.value = true;
  form.value.error = '';
  // 清除之前存储的过期 token，避免旧 token 导致 401 干扰本次登录
  auth.logout();
  try {
    await auth.login(form.value.username.trim(), form.value.password);
    router.push('/dashboard');
  } catch (e) {
    const err = e as { detail?: string; message?: string };
    form.value.error = err?.detail || err?.message || '登录失败';
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-page">
    <!-- 登录前也能切主题（选择存 localStorage，登录后继续生效） -->
    <div class="login-theme"><ThemeToggle /></div>
    <div class="login-card">
      <div class="login-brand">⚡ FastAPI 管理台</div>
      <form @submit.prevent="doSubmit" class="login-form">
        <h2>账号密码登录</h2>
        <Input v-model:value="form.username" placeholder="用户名" :prefix="'👤'" />
        <Input.Password v-model:value="form.password" placeholder="密码" @keyup.enter="doSubmit" />
        <Alert v-if="form.error" :type="'error'" :message="form.error" show-icon class="login-alert" />
        <Button type="primary" block :loading="loading" @click="doSubmit" class="login-submit">
          登 录
        </Button>
      </form>
      <div class="login-tip" @click="router.push('/auth/onboard')" style="cursor: pointer;">
        首次部署？创建第一个超级管理员
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1f2937 0%, #111827 100%);
  padding: 24px;
}
.login-card {
  width: 100%;
  max-width: 400px;
  background: var(--app-panel);
  border-radius: 12px;
  padding: 40px 32px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.15);
}
.login-theme {
  position: absolute;
  top: 16px;
  right: 20px;
}
/* 登录页渐变背景单独配色（卡片已经走 --app-panel） */
html[data-theme='dark'] .login-page {
  background: linear-gradient(135deg, #1b2430 0%, #0b0f16 100%);
}
.login-brand {
  font-size: 22px;
  font-weight: 700;
  color: var(--app-fg);
  margin-bottom: 24px;
  text-align: center;
}
.login-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.login-form h2 {
  margin: 0 0 8px;
  color: var(--app-fg);
}
.login-submit {
  margin-top: 8px;
  height: 42px;
  border-radius: 8px;
}
.login-tip {
  margin-top: 16px;
  font-size: 13px;
  color: var(--app-muted);
  text-align: center;
}
</style>
