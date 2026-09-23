<script setup lang="ts">
/**
 * Onboard 页 —— 首次引导：创建唯一一个超级管理员。
 *
 * 后端 RBAC 后该接口只做一件事：建库内第一个账号并绑定内置 superadmin，
 * 需要引导密钥（APP_ADMIN_API_KEY）作为 X-API-Key 头；已有账号时直接 409。
 * 响应体不含 token，因此创建成功后再走一次登录拿会话与权限集。
 */
import { reactive, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useAuthStore } from '@/store/modules/auth';
import { Alert, Button, Card, Divider, Form, Input, message } from 'ant-design-vue';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

const form = reactive({ bootstrap: '', username: '', name: '', password: '' });
const loading = ref(false);
const errorMsg = ref('');

async function doOnboard() {
  if (!form.bootstrap) return message.warning('请填写引导密钥（后端 APP_ADMIN_API_KEY）');
  if (!form.username || !form.password || !form.name) return message.warning('请填写全部字段');
  if (form.password.length < 8) return message.warning('密码至少 8 位');
  loading.value = true;
  errorMsg.value = '';
  try {
    // 清掉旧登录态，避免过期 token 把本次流程变成 401
    auth.logout();
    await auth.onboard(form.username.trim(), form.password, form.name.trim(), form.bootstrap.trim());
    const redirect = (route.query.redirect as string) || '/dashboard';
    message.success('创建成功，已自动登录');
    router.push(redirect);
  } catch (e) {
    const err = e as { detail?: string; message?: string };
    const raw = err?.detail || err?.message || '创建失败';
    // 已有账号时直接把用户带去登录，不让他在引导页反复碰壁
    if (raw.includes('already exist')) {
      message.warning('已存在管理员账号，请直接登录');
      router.push('/auth/login');
      return;
    }
    errorMsg.value = raw;
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-page">
    <Card class="login-card">
      <div class="login-title">
        <span class="logo">👑</span>
        <span>创建首个管理员</span>
      </div>
      <Divider dashed />

      <Alert
        v-if="errorMsg"
        type="error"
        show-icon
        :message="errorMsg"
        class="login-alert"
      />
      <Alert
        v-else
        type="info"
        show-icon
        class="login-alert"
        message="仅用于空库首次引导"
        description="引导完成后，后续账号请在「账号管理」里新建（需 account:manage 权限）。"
      />

      <Form layout="vertical">
        <Form.Item label="引导密钥" required>
          <Input.Password
            v-model:value="form.bootstrap"
            placeholder="后端 APP_ADMIN_API_KEY 的值"
            size="large"
          />
        </Form.Item>

        <Form.Item label="管理员用户名" required>
          <Input v-model:value="form.username" placeholder="登录用户名" size="large" />
        </Form.Item>

        <Form.Item label="显示姓名" required>
          <Input v-model:value="form.name" placeholder="展示名，默认与用户名相同" size="large" />
        </Form.Item>

        <Form.Item label="登录密码" required>
          <Input.Password v-model:value="form.password" placeholder="≥8 位" size="large" />
        </Form.Item>

        <Button
          type="primary"
          block
          size="large"
          :loading="loading"
          @click="doOnboard"
        >
          创建并登录
        </Button>

        <div class="login-tip">
          <a @click="router.push('/auth/login')">已有 admin？去登录</a>
        </div>
      </Form>
    </Card>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
  padding: 24px;
}
/* 窄屏（笔记本小分辨率 / 缩放模式）下卡片不超出视口宽 */
.login-card { width: min(420px, 100%); border-radius: 12px; box-shadow: 0 8px 40px rgba(0, 0, 0, 0.3); }
.login-title { font-size: 22px; font-weight: 700; color: var(--app-fg); text-align: center; }
.login-title .logo { margin-right: 8px; }
.login-alert { margin-bottom: 16px; }
.login-tip { margin-top: 12px; font-size: 12px; color: var(--app-muted); text-align: center; }
</style>
