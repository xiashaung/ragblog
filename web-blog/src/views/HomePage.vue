<script setup lang="ts">
// 首页、标签页与分类页共用：顶部横向 Hero（左站点主张 + 右 AI 搜索入口），下方封面卡片网格。
// 分类导航已上收到顶栏下拉；标签作为横向浏览入口收在网格底部。
// 路由 /、/tag/:tag、/category/:slug 都指向本组件，参数变化时重新拉列表。
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import dayjs from 'dayjs';
import { FileTextOutlined, EyeOutlined, RobotOutlined, SearchOutlined, StarFilled } from '@ant-design/icons-vue';
import { blogApi, type PostItem, type TagItem } from '@/api';
import { categories, loadCategories } from '@/stores/categories';

const PAGE_SIZE = 10;

const route = useRoute();
const router = useRouter();

const posts = ref<PostItem[]>([]);
const total = ref(0);
const offset = ref(0);
const tags = ref<TagItem[]>([]);
const loading = ref(false);
const error = ref('');
// Hero 右侧搜索框的关键词：回车/点按钮跳 /search，带 ?q= 让 AI 对话页自动提问
const heroQuery = ref('');

/** 当前生效的标签筛选（/tag/:tag 或无筛选） */
function currentTag(): string | undefined {
  return route.name === 'tag' && typeof route.params.tag === 'string' ? route.params.tag : undefined;
}

/** 当前生效的分类筛选（/category/:slug） */
function currentCategory(): string | undefined {
  return route.name === 'category' && typeof route.params.slug === 'string' ? route.params.slug : undefined;
}

/** 顶部筛选横幅：标签页或分类页时展示 */
const filterBanner = computed(() => {
  if (currentCategory()) {
    const name = categories.value.find((c) => c.slug === currentCategory())?.name || currentCategory();
    return { type: 'category' as const, text: `分类「${name}」下的文章` };
  }
  const tag = currentTag();
  return tag ? { type: 'tag' as const, text: `标签「${tag}」下的文章` } : null;
});

async function loadPosts(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    const res = await blogApi.posts({
      tag: currentTag(),
      category: currentCategory(),
      offset: offset.value,
      limit: PAGE_SIZE,
    });
    posts.value = res.items;
    total.value = res.total;
  } catch (e) {
    error.value = (e as Error).message || '加载文章列表失败';
  } finally {
    loading.value = false;
  }
}

async function loadSide(): Promise<void> {
  // 分类走共享 store（顶栏已拉取）；这里只补标签，失败不阻塞正文
  loadCategories();
  try {
    const t = await blogApi.tags();
    tags.value = t.items;
  } catch {
    /* 侧栏失败不阻塞正文 */
  }
}

function goPost(slug: string): void {
  router.push({ name: 'post', params: { slug } });
}

function goTag(tag: string): void {
  router.push({ name: 'tag', params: { tag } });
}

function goCategory(slug: string): void {
  router.push({ name: 'category', params: { slug } });
}

function clearFilter(): void {
  router.push({ name: 'home' });
}

function goSearch(): void {
  const q = heroQuery.value.trim();
  router.push({ name: 'search', query: q ? { q } : {} });
}

function onPage(page: number): void {
  offset.value = (page - 1) * PAGE_SIZE;
  loadPosts();
  window.scrollTo({ top: 0 });
}

function fmtTime(ts?: number | null): string {
  return ts ? dayjs(ts * 1000).format('YYYY-MM-DD') : '';
}

watch(() => route.fullPath, () => {
  offset.value = 0;
  loadPosts();
});

onMounted(() => {
  loadPosts();
  loadSide();
});
</script>

<template>
  <div class="home">
    <!-- Hero：横向信息区 —— 左侧站点主张与统计，右侧 AI 搜索入口 -->
    <section v-if="route.name === 'home'" class="hero">
      <div class="hero-text">
        <p class="hero-kicker">PERSONAL TECH BLOG · RAG POWERED</p>
        <h1 class="hero-title">每一段技术思考，都能被找到</h1>
        <p class="hero-desc">写作 · 归类 · 语义检索 —— 支持自然语言搜索与 AI 追问，答案附引用来源。</p>
        <div class="hero-stats">
          <div class="hero-stat">
            <span class="hero-num">{{ total }}</span>
            <span class="hero-label">篇文章</span>
          </div>
          <div class="hero-stat">
            <span class="hero-num">{{ categories.length }}</span>
            <span class="hero-label">个分类</span>
          </div>
          <div class="hero-stat">
            <span class="hero-num">{{ tags.length }}</span>
            <span class="hero-label">个标签</span>
          </div>
        </div>
      </div>
      <div class="hero-search">
        <div class="hero-search-title"><RobotOutlined /> 试试 AI 语义检索</div>
        <a-input
          v-model:value="heroQuery"
          size="large"
          placeholder="用一句话描述你想找的文章…"
          allow-clear
          @press-enter="goSearch"
        >
          <template #prefix><SearchOutlined /></template>
        </a-input>
        <a-button type="primary" size="large" block class="hero-search-btn" @click="goSearch">
          搜索本站
        </a-button>
        <p class="hero-search-hint">回车直接进入 AI 对话，边检索边流式作答</p>
      </div>
    </section>

    <a-alert v-if="error" type="error" :message="error" show-icon class="list-alert" />

    <div v-if="filterBanner" class="tag-banner">
      <span>{{ filterBanner.text }}（{{ total }} 篇）</span>
      <a-button type="link" size="small" @click="clearFilter">清除筛选</a-button>
    </div>

    <a-spin :spinning="loading">
      <!-- 封面卡片网格：一行 lg 3 / sm 2 / xs 1，封面置顶、正文在下 -->
      <a-row :gutter="[20, 20]">
        <a-col v-for="post in posts" :key="post.slug" :xs="24" :sm="12" :lg="8">
          <article class="blog-card post-grid-card" @click="goPost(post.slug)">
            <div class="grid-cover">
              <img v-if="post.cover_url" :src="post.cover_url" :alt="post.title" loading="lazy" />
              <div v-else class="cover-ph"><FileTextOutlined /></div>
              <a-tag v-if="post.featured" color="red" class="cover-badge"><StarFilled /> 置顶</a-tag>
              <a-tag
                v-if="post.category"
                color="blue"
                class="cover-cat"
                @click.stop="goCategory(post.category.slug)"
              >
                {{ post.category.name }}
              </a-tag>
            </div>
            <div class="grid-body">
              <h2 class="grid-title">
                <router-link :to="{ name: 'post', params: { slug: post.slug } }">{{ post.title }}</router-link>
              </h2>
              <p class="grid-summary">{{ post.summary }}</p>
              <div class="grid-meta">
                <span><FileTextOutlined /> {{ fmtTime(post.published_at) }}</span>
                <span><EyeOutlined /> {{ post.view_count }}</span>
              </div>
              <div v-if="post.tags.length" class="grid-tags">
                <a-tag v-for="t in post.tags" :key="t" class="post-tag" @click.stop="goTag(t)">{{ t }}</a-tag>
              </div>
            </div>
          </article>
        </a-col>
      </a-row>

      <a-empty v-if="!loading && !posts.length && !error" description="还没有文章" />

      <div v-if="total > PAGE_SIZE" class="pager">
        <a-pagination
          :current="offset / PAGE_SIZE + 1"
          :page-size="PAGE_SIZE"
          :total="total"
          hide-on-single-page
          @change="onPage"
        />
      </div>
    </a-spin>

    <!-- 标签云：分类已上收顶栏，标签作为横向浏览入口收在网格底部 -->
    <section v-if="route.name === 'home'" class="tag-section">
      <h3 class="side-title">按标签浏览</h3>
      <div class="tag-cloud">
        <a-tag
          v-for="t in tags"
          :key="t.name"
          :color="t.name === currentTag() ? 'blue' : undefined"
          class="cloud-tag"
          @click="goTag(t.name)"
        >
          {{ t.name }} <span class="tag-count">{{ t.count }}</span>
        </a-tag>
        <span v-if="!tags.length" class="side-empty">暂无标签</span>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* --- Hero：横向信息区，左文右搜索面板 --- */
.home {
  min-height: 240px;
}

.hero {
  display: flex;
  align-items: stretch;
  gap: 28px;
  padding: 30px 0 10px;
}

.hero-text {
  flex: 1;
  min-width: 0;
}

.hero-kicker {
  margin: 0;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 12px;
  letter-spacing: 3px;
  color: var(--primary);
  opacity: 0.9;
}

.hero-title {
  margin: 12px 0 10px;
  font-size: clamp(26px, 4vw, 40px);
  font-weight: 800;
  letter-spacing: 1px;
  line-height: 1.25;
  background: var(--grad);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.hero-desc {
  margin: 0;
  max-width: 520px;
  color: var(--text-soft);
  font-size: 15px;
  line-height: 1.7;
}

.hero-stats {
  display: flex;
  gap: 14px;
  margin-top: 22px;
}

.hero-stat {
  min-width: 92px;
  padding: 12px 18px;
  background: var(--bg-card);
  backdrop-filter: var(--card-glass);
  -webkit-backdrop-filter: var(--card-glass);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: var(--shadow);
}

.hero-num {
  display: block;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 24px;
  font-weight: 700;
  line-height: 1.2;
  background: var(--grad);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.hero-label {
  font-size: 12.5px;
  color: var(--text-soft);
}

/* Hero 右侧：AI 搜索入口面板 */
.hero-search {
  width: 340px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 22px;
  background: var(--bg-card);
  backdrop-filter: var(--card-glass);
  -webkit-backdrop-filter: var(--card-glass);
  border: 1px solid var(--border);
  border-radius: 16px;
  box-shadow: var(--shadow);
}

.hero-search-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: var(--text);
}

.hero-search-btn {
  background: var(--grad);
  border: none;
}

.hero-search-hint {
  margin: 0;
  font-size: 12px;
  color: var(--text-soft);
}

.list-alert {
  margin-bottom: 16px;
}

.tag-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 16px;
  margin-bottom: 16px;
  color: var(--text-soft);
}

/* --- 封面卡片网格 --- */
.post-grid-card {
  height: 100%;
  padding: 0;
  overflow: hidden;
  cursor: pointer;
  display: flex;
  flex-direction: column;
}

.grid-cover {
  position: relative;
  aspect-ratio: 16 / 9;
  background: var(--bg-soft);
  overflow: hidden;
}

.grid-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  transition: transform 0.3s ease;
}

.post-grid-card:hover .grid-cover img {
  transform: scale(1.05);
}

.cover-ph {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 40px;
  color: #fff;
  background: var(--grad);
  opacity: 0.9;
}

.cover-badge {
  position: absolute;
  top: 10px;
  left: 10px;
  margin: 0;
}

.cover-cat {
  position: absolute;
  top: 10px;
  right: 10px;
  margin: 0;
  cursor: pointer;
}

.grid-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px 18px 18px;
}

.grid-title {
  margin: 0;
  font-size: 18px;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.grid-title a {
  color: var(--text);
}

.grid-title a:hover {
  color: var(--primary);
  text-decoration: none;
}

.grid-summary {
  margin: 0;
  color: var(--text-soft);
  font-size: 13.5px;
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.grid-meta {
  display: flex;
  gap: 14px;
  align-items: center;
  margin-top: auto;
  color: var(--text-soft);
  font-size: 12.5px;
}

.grid-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.post-tag {
  cursor: pointer;
}

.pager {
  margin-top: 28px;
  display: flex;
  justify-content: center;
}

/* --- 标签云：网格下方的横向浏览区 --- */
.tag-section {
  margin-top: 40px;
  padding-top: 24px;
  border-top: 1px solid var(--border);
}

.side-title {
  margin: 0 0 14px;
  font-size: 16px;
}

.tag-cloud {
  display: flex;
  flex-wrap: wrap;
}

.cloud-tag {
  cursor: pointer;
  margin-bottom: 8px;
}

.tag-count {
  color: var(--text-soft);
  font-size: 12px;
}

.side-empty {
  color: var(--text-soft);
  font-size: 13px;
}

@media (max-width: 860px) {
  .hero {
    flex-direction: column;
    gap: 20px;
  }

  .hero-search {
    width: 100%;
  }
}

@media (max-width: 720px) {
  .hero {
    padding-top: 20px;
  }

  .hero-stat {
    min-width: 80px;
    padding: 10px 14px;
  }
}
</style>
