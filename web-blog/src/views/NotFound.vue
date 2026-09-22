<script setup lang="ts">
// 404：兜底路由，给个回家的按钮就好。
import { useRouter } from 'vue-router';
import { HomeOutlined } from '@ant-design/icons-vue';

const router = useRouter();
</script>

<template>
  <a-result status="404" title="404" sub-title="你访问的页面不存在，或者文章已被下架。">
    <template #extra>
      <a-button type="primary" @click="router.push({ name: 'home' })">
        <template #icon><HomeOutlined /></template>
        返回首页
      </a-button>
    </template>
  </a-result>
</template>
