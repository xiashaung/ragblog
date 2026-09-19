<script setup lang="ts">
/** 404 页 —— 未匹配路由。 */
import { Button, Result } from 'ant-design-vue';
import { useRouter } from 'vue-router';
const router = useRouter();
</script>

<template>
  <div class="page-card">
    <Result status="404" sub-title="对不起，访问的页面不存在。">
      <template #extra>
        <Button type="primary" @click="router.push('/dashboard')">回到概览</Button>
      </template>
    </Result>
  </div>
</template>
