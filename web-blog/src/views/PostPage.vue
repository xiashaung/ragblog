<script setup lang="ts">
// 文章页：按 slug 拉详情（后端顺便 view_count +1），markdown-it 渲染正文。
import { ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import dayjs from 'dayjs';
import { ArrowLeftOutlined, EyeOutlined, FileTextOutlined } from '@ant-design/icons-vue';
import { blogApi, type PostDetail } from '@/api';
import { renderMarkdown } from '@/utils/md';

const route = useRoute();
const router = useRouter();

const post = ref<PostDetail | null>(null);
const loading = ref(false);
const error = ref('');

const html = ref('');

async function load(slug: string): Promise<void> {
  loading.value = true;
  error.value = '';
  post.value = null;
  try {
    const data = await blogApi.post(slug);
    post.value = data;
    // 正文渲染一次成 HTML；markdown-it 已转义原始标签（html:false）
    html.value = renderMarkdown(data.content);
  } catch (e) {
    error.value = (e as Error).message || '加载文章失败';
  } finally {
    loading.value = false;
  }
}

function goTag(tag: string): void {
  router.push({ name: 'tag', params: { tag } });
}

function goCategory(slug: string): void {
  router.push({ name: 'category', params: { slug } });
}

watch(() => route.params.slug, (slug) => {
  if (typeof slug === 'string' && slug) load(slug);
}, { immediate: true });
</script>

<template>
  <a-spin :spinning="loading">
    <a-alert v-if="error" type="error" :message="error" show-icon>
      <template #description>
        <a-button type="link" size="small" @click="router.push({ name: 'home' })">
          <template #icon><ArrowLeftOutlined /></template>
          返回首页
        </a-button>
      </template>
    </a-alert>

    <article v-else-if="post" class="post-body">
      <header class="post-header">
        <h1 class="post-title">{{ post.title }}</h1>
        <div class="card-meta post-meta">
          <span><FileTextOutlined /> {{ post.published_at ? dayjs(post.published_at * 1000).format('YYYY-MM-DD HH:mm') : '' }}</span>
          <span><EyeOutlined /> {{ post.view_count }} 次阅读</span>
          <a-tag v-if="post.category" color="blue" class="post-tag" @click="goCategory(post.category.slug)">
            {{ post.category.name }}
          </a-tag>
          <a-tag v-for="t in post.tags" :key="t" class="post-tag" @click="goTag(t)">{{ t }}</a-tag>
        </div>
        <img v-if="post.cover_url" :src="post.cover_url" class="post-cover" alt="" />
      </header>

      <!-- eslint-disable-next-line vue/no-v-html -- 正文经 markdown-it（html:false）转义，原始标签不可执行 -->
      <div class="markdown-body" v-html="html" />
    </article>
  </a-spin>
</template>

<style scoped>
.post-header {
  margin-bottom: 24px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--border);
}

.post-title {
  margin: 0 0 12px;
  font-size: clamp(24px, 3.6vw, 34px);
  font-weight: 800;
  letter-spacing: 0.5px;
  line-height: 1.35;
}

.post-meta {
  margin-bottom: 12px;
}

.post-tag {
  cursor: pointer;
}

.post-cover {
  margin-top: 16px;
  width: 100%;
  max-height: 360px;
  object-fit: cover;
  border-radius: 10px;
}
</style>
