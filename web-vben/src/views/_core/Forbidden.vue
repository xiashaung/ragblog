<script setup lang="ts">
/**
 * 403 页 —— 路由守卫发现当前账号缺少页面所需权限码时跳这里。
 * 真正的拦截仍在后端（require_permission），这里只是给出可读的提示。
 */
import { computed } from 'vue';
import { Button, Result, Space, Tag } from 'ant-design-vue';
import { useRoute, useRouter } from 'vue-router';
import { useAuthStore } from '@/store/modules/auth';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

const needed = computed<string[]>(() => {
  const from = route.query.from as string | undefined;
  const target = router.getRoutes().find((r) => r.path === from);
  return (target?.meta?.permissions as string[] | undefined) ?? [];
});
</script>

<template>
  <Result status="403" title="403" :sub-title="`当前账号【${auth.userInfo?.username || '未知'}】没有访问该页面的权限`">
    <template #extra>
      <div style="margin-bottom: 16px">
        <Space wrap>
          <span>需要权限：</span>
          <Tag v-for="code in needed" :key="code" color="red">{{ code }}</Tag>
          <span v-if="!needed.length">（无特定权限要求）</span>
        </Space>
      </div>
      <Space>
        <Button type="primary" @click="router.push('/dashboard')">回到概览</Button>
        <Button @click="router.push('/auth/login')">换个账号登录</Button>
      </Space>
    </template>
  </Result>
</template>
