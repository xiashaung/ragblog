<script setup lang="ts">
// 文章页：按 slug 拉详情（后端顺便 view_count +1），markdown-it 渲染正文。
// 阅读体验件：右侧浮动目录（滚动高亮）、预计阅读时长、代码块一键复制、
// 上/下篇导航与「相关文章」推荐——导航与推荐都静默失败，不打断读正文。
import { nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import dayjs from 'dayjs';
import {
  ArrowLeftOutlined,
  ClockCircleOutlined,
  EyeOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  LeftOutlined,
  RightOutlined,
} from '@ant-design/icons-vue';
import {
  blogApi,
  type Neighbors,
  type PostDetail,
  type PostItem,
} from '@/api';
import { attachHeadingAnchors, renderMarkdown, type TocItem } from '@/utils/md';

const route = useRoute();
const router = useRouter();

const post = ref<PostDetail | null>(null);
const loading = ref(false);
const error = ref('');

const html = ref('');
const toc = ref<TocItem[]>([]);
const activeToc = ref('');
const readMinutes = ref(0);
const neighbors = ref<Neighbors | null>(null);
const related = ref<PostItem[]>([]);
const bodyEl = ref<HTMLElement | null>(null);

async function load(slug: string): Promise<void> {
  loading.value = true;
  error.value = '';
  post.value = null;
  toc.value = [];
  activeToc.value = '';
  neighbors.value = null;
  related.value = [];
  try {
    const data = await blogApi.post(slug);
    post.value = data;
    // 正文渲染一次成 HTML；markdown-it 已转义原始标签（html:false）
    const anchored = attachHeadingAnchors(renderMarkdown(data.content));
    html.value = anchored.html;
    toc.value = anchored.toc;
    // 中文阅读速度按 350 字/分钟估算，最少 1 分钟
    readMinutes.value = Math.max(1, Math.round((data.content || '').length / 350));
    // 导航与推荐并行补拉，失败留空不影响正文
    blogApi
      .neighbors(slug)
      .then((n) => {
        neighbors.value = n;
      })
      .catch(() => {});
    blogApi
      .related(slug)
      .then((r) => {
        related.value = r.items;
      })
      .catch(() => {});
  } catch (e) {
    error.value = (e as Error).message || '加载文章失败';
  } finally {
    loading.value = false;
  }
}

/** 给每个代码块注入「复制」按钮（v-html 的动态 DOM，事件走容器委托） */
function injectCopyButtons(): void {
  bodyEl.value
    ?.querySelectorAll('.markdown-body pre')
    .forEach((pre) => {
      if (pre.querySelector('.copy-btn')) return;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'copy-btn';
      btn.textContent = '复制';
      pre.appendChild(btn);
    });
}

async function onBodyClick(e: MouseEvent): Promise<void> {
  const btn = (e.target as HTMLElement).closest?.('.copy-btn');
  if (!btn) return;
  const code = btn.closest('pre')?.querySelector('code')?.textContent ?? '';
  try {
    await navigator.clipboard.writeText(code);
    btn.textContent = '已复制 ✓';
  } catch {
    btn.textContent = '复制失败';
  }
  window.setTimeout(() => {
    btn.textContent = '复制';
  }, 1500);
}

function jumpToc(item: TocItem): void {
  activeToc.value = item.id;
  document.getElementById(item.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** 滚动高亮：取「顶部已越过阈值」的最后一个标题 */
function onScroll(): void {
  if (!toc.value.length) return;
  let current = '';
  for (const item of toc.value) {
    const el = document.getElementById(item.id);
    if (el && el.getBoundingClientRect().top <= 96) current = item.id;
  }
  activeToc.value = current;
}

function goTag(tag: string): void {
  router.push({ name: 'tag', params: { tag } });
}

function goCategory(slug: string): void {
  router.push({ name: 'category', params: { slug } });
}

function fmtDate(ts?: number | null): string {
  return ts ? dayjs(ts * 1000).format('YYYY-MM-DD') : '';
}

watch(
  () => route.params.slug,
  (slug) => {
    if (typeof slug === 'string' && slug) load(slug);
  },
  { immediate: true },
);

watch(html, () => nextTick(injectCopyButtons));

onMounted(() => window.addEventListener('scroll', onScroll, { passive: true }));
onBeforeUnmount(() => window.removeEventListener('scroll', onScroll));
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

    <a-row v-else-if="post" :gutter="24">
      <a-col :xs="24" :lg="17">
        <article ref="bodyEl" class="post-body" @click="onBodyClick">
          <header class="post-header">
            <h1 class="post-title">{{ post.title }}</h1>
            <div class="card-meta post-meta">
              <span><FileTextOutlined /> {{ post.published_at ? dayjs(post.published_at * 1000).format('YYYY-MM-DD HH:mm') : '' }}</span>
              <span><ClockCircleOutlined /> 约 {{ readMinutes }} 分钟</span>
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

        <!-- 相关文章推荐：同分类/共同标签加权 -->
        <section v-if="related.length" class="blog-card related-card">
          <h3 class="related-title"><FolderOpenOutlined /> 相关文章</h3>
          <router-link
            v-for="r in related"
            :key="r.slug"
            :to="{ name: 'post', params: { slug: r.slug } }"
            class="related-item"
          >
            <span class="related-name">{{ r.title }}</span>
            <span class="related-meta">{{ fmtDate(r.published_at) }} · {{ r.view_count }} 次</span>
          </router-link>
        </section>

        <!-- 上/下篇：prev 是更早发布的文章 -->
        <nav v-if="neighbors && (neighbors.prev || neighbors.next)" class="post-nav">
          <router-link
            v-if="neighbors.prev"
            :to="{ name: 'post', params: { slug: neighbors.prev.slug } }"
            class="post-nav-item"
          >
            <span class="pn-label"><LeftOutlined /> 上一篇</span>
            <span class="pn-title">{{ neighbors.prev.title }}</span>
          </router-link>
          <span v-else class="post-nav-item placeholder" />
          <router-link
            v-if="neighbors.next"
            :to="{ name: 'post', params: { slug: neighbors.next.slug } }"
            class="post-nav-item next"
          >
            <span class="pn-label">下一篇 <RightOutlined /></span>
            <span class="pn-title">{{ neighbors.next.title }}</span>
          </router-link>
          <span v-else class="post-nav-item placeholder" />
        </nav>
      </a-col>

      <!-- 目录侧栏：大屏 sticky，内容不足或窄屏隐藏 -->
      <a-col v-if="toc.length" :xs="0" :sm="0" :lg="7">
        <div class="toc-card">
          <h4 class="toc-heading">目录</h4>
          <div class="toc-scroll">
            <a
              v-for="item in toc"
              :key="item.id"
              class="toc-item"
              :class="{ active: activeToc === item.id, deep: item.level >= 3 }"
              @click="jumpToc(item)"
            >{{ item.text }}</a>
          </div>
        </div>
      </a-col>
    </a-row>
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

/* --- 目录侧栏 --- */
.toc-card {
  position: sticky;
  top: 84px;
  background: var(--bg-card);
  backdrop-filter: var(--card-glass);
  -webkit-backdrop-filter: var(--card-glass);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow);
  padding: 16px 8px 16px 16px;
}

.toc-heading {
  margin: 0 12px 10px 0;
  font-size: 14px;
  color: var(--text-soft);
  letter-spacing: 1px;
}

.toc-scroll {
  max-height: calc(100vh - 200px);
  overflow-y: auto;
  scrollbar-width: thin;
}

.toc-item {
  display: block;
  padding: 5px 12px 5px 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--text-soft);
  border-left: 2px solid transparent;
  padding-left: 12px;
  margin-left: -16px;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.toc-item:hover {
  color: var(--text);
  text-decoration: none;
}

.toc-item.active {
  color: var(--primary);
  border-left-color: var(--primary);
  font-weight: 600;
}

.toc-item.deep {
  padding-left: 26px;
}

/* --- 相关文章 --- */
.related-card {
  margin-top: 16px;
}

.related-title {
  margin: 0 0 10px;
  font-size: 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.related-item {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 16px;
  padding: 9px 2px;
  border-top: 1px dashed var(--border);
  color: var(--text);
  transition: color 0.15s ease;
}

.related-item:first-of-type {
  border-top: none;
}

.related-item:hover {
  color: var(--primary);
  text-decoration: none;
}

.related-name {
  font-weight: 600;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.related-meta {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-soft);
}

/* --- 上/下篇 --- */
.post-nav {
  display: flex;
  gap: 12px;
  margin-top: 16px;
}

.post-nav-item {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 16px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  color: var(--text);
  transition: border-color 0.15s ease, transform 0.15s ease;
}

.post-nav-item:hover {
  text-decoration: none;
  border-color: var(--primary);
  transform: translateY(-1px);
}

.post-nav-item.next {
  text-align: right;
}

.post-nav-item.placeholder {
  visibility: hidden;
}

.pn-label {
  font-size: 12px;
  color: var(--text-soft);
}

.pn-title {
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 960px) {
  .post-nav {
    flex-direction: column;
  }

  .post-nav-item.placeholder {
    display: none;
  }
}
</style>
