<script setup lang="ts">
// 首页、标签页与分类页共用：左侧文章分页列表，右侧分类 + 标签侧栏。
// 路由 /、/tag/:tag、/category/:slug 都指向本组件，参数变化时重新拉列表。
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import dayjs from 'dayjs';
import { FileTextOutlined, EyeOutlined, FolderOutlined, StarFilled } from '@ant-design/icons-vue';
import { blogApi, type CategoryItem, type PostItem, type TagItem } from '@/api';

const PAGE_SIZE = 10;

const route = useRoute();
const router = useRouter();

const posts = ref<PostItem[]>([]);
const total = ref(0);
const offset = ref(0);
const tags = ref<TagItem[]>([]);
const categories = ref<CategoryItem[]>([]);
const loading = ref(false);
const error = ref('');

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
  try {
    const [t, c] = await Promise.all([blogApi.tags(), blogApi.categories()]);
    tags.value = t.items;
    categories.value = c.items;
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
  <div>
    <!-- 首页首屏 Hero：渐变大标题 + 站点统计，标签/分类筛选页不展示 -->
    <section v-if="route.name === 'home'" class="hero">
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
      <div class="hero-divider" />
    </section>

    <a-row :gutter="24">
    <a-col :xs="24" :md="17">
      <a-alert v-if="error" type="error" :message="error" show-icon class="list-alert" />

      <div v-if="filterBanner" class="tag-banner">
        <span>{{ filterBanner.text }}（{{ total }} 篇）</span>
        <a-button type="link" size="small" @click="clearFilter">清除筛选</a-button>
      </div>

      <a-spin :spinning="loading">
        <!-- 整卡可点进详情；卡片内标签/分类已 click.stop，不会误冒泡 -->
        <div
          v-for="post in posts"
          :key="post.slug"
          class="blog-card post-card"
          @click="goPost(post.slug)"
        >
          <div class="post-card-main">
            <h2 class="card-title">
              <router-link :to="{ name: 'post', params: { slug: post.slug } }">
                <a-tag v-if="post.featured" color="red"><StarFilled /> 置顶</a-tag>
                {{ post.title }}
              </router-link>
            </h2>
            <p class="card-summary">{{ post.summary }}</p>
            <div class="card-meta">
              <span><FileTextOutlined /> {{ fmtTime(post.published_at) }}</span>
              <span><EyeOutlined /> {{ post.view_count }}</span>
              <a-tag v-if="post.category" color="blue" class="post-tag" @click.stop="goCategory(post.category.slug)">
                {{ post.category.name }}
              </a-tag>
              <a-tag v-for="t in post.tags" :key="t" class="post-tag" @click.stop="goTag(t)">{{ t }}</a-tag>
            </div>
          </div>
          <img v-if="post.cover_url" :src="post.cover_url" class="cover-thumb" alt="" loading="lazy" />
        </div>

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
    </a-col>

    <a-col :xs="24" :md="7">
      <div class="blog-card sidebar-card">
        <h3 class="side-title"><FolderOutlined /> 分类</h3>
        <div class="tag-cloud">
          <a-tag
            v-for="c in categories"
            :key="c.slug"
            :color="c.slug === currentCategory() ? 'blue' : undefined"
            class="cloud-tag"
            @click="goCategory(c.slug)"
          >
            {{ c.name }} <span class="tag-count">{{ c.post_count }}</span>
          </a-tag>
          <span v-if="!categories.length" class="side-empty">暂无分类</span>
        </div>
      </div>
      <div class="blog-card sidebar-card">
        <h3 class="side-title">标签</h3>
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
      </div>
    </a-col>
    </a-row>
  </div>
</template>

<style scoped>
/* --- 首页 Hero：渐变标题 + 辉光统计，营造首屏纵深 --- */
.hero {
  padding: 44px 0 8px;
  text-align: center;
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
  margin: 14px 0 10px;
  font-size: clamp(30px, 5vw, 44px);
  font-weight: 800;
  letter-spacing: 1px;
  line-height: 1.25;
  background: var(--grad);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.hero-desc {
  margin: 0 auto;
  max-width: 560px;
  color: var(--text-soft);
  font-size: 15px;
}

.hero-stats {
  display: flex;
  justify-content: center;
  gap: 18px;
  margin-top: 26px;
}

.hero-stat {
  min-width: 104px;
  padding: 14px 22px;
  background: var(--bg-card);
  backdrop-filter: var(--card-glass);
  -webkit-backdrop-filter: var(--card-glass);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow);
}

.hero-num {
  display: block;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 26px;
  font-weight: 700;
  line-height: 1.2;
  background: var(--grad);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.hero-label {
  font-size: 13px;
  color: var(--text-soft);
}

/* Hero 与列表之间的渐隐分割线 */
.hero-divider {
  height: 1px;
  margin: 30px auto 6px;
  max-width: 720px;
  background: linear-gradient(90deg, transparent, var(--border) 20%, var(--border) 80%, transparent);
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

.post-card {
  display: flex;
  gap: 20px;
  align-items: flex-start;
  cursor: pointer;
}

.post-card-main {
  flex: 1;
  min-width: 0;
}

.post-tag {
  cursor: pointer;
}

.pager {
  margin-top: 24px;
  display: flex;
  justify-content: center;
}

.side-title {
  margin: 0 0 12px;
  font-size: 16px;
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

@media (max-width: 720px) {
  .sidebar-card {
    margin-top: 16px;
  }

  .hero {
    padding-top: 24px;
  }

  .hero-stat {
    min-width: 84px;
    padding: 10px 14px;
  }
}
</style>
