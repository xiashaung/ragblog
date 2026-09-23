<script setup lang="ts">
// 首页、标签页与分类页共用：顶部横向 Hero（左站点主张 + 右 AI 搜索入口），下方封面卡片网格。
// 分类与标签导航都已上收到顶栏下拉；页面底层铺一层动态背景（漂移光斑 + 星座粒子）。
// 路由 /、/tag/:tag、/category/:slug 都指向本组件，参数变化时重新拉列表。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import dayjs from 'dayjs';
import { FileTextOutlined, EyeOutlined, RobotOutlined, SearchOutlined, StarFilled } from '@ant-design/icons-vue';
import { blogApi, type PostItem } from '@/api';
import { categories, loadCategories } from '@/stores/categories';
import { tags, loadTags } from '@/stores/tags';

const PAGE_SIZE = 10;

const route = useRoute();
const router = useRouter();

const posts = ref<PostItem[]>([]);
const total = ref(0);
const offset = ref(0);
const loading = ref(false);
const error = ref('');
// Hero 右侧搜索框的关键词：回车/点按钮跳 /search，带 ?q= 让 AI 对话页自动提问
const heroQuery = ref('');

// --- 首页动态背景：漂移光斑（CSS）+ 画布「星座」粒子（JS，随鼠标连线）---
// 只在 HomePage 挂载期间运行；颜色从 CSS 变量读，切明暗时自动跟随；尊重 prefers-reduced-motion。
const fxCanvas = ref<HTMLCanvasElement | null>(null);
let fxRaf = 0;
let fxThemeObserver: MutationObserver | null = null;
let fxCleanup: (() => void) | null = null;

function initFx(): void {
  const canvas = fxCanvas.value;
  const ctx = canvas?.getContext('2d');
  if (!canvas || !ctx) return;
  // 取非空别名供内层闭包使用：TS 不在嵌套函数里保留上方控制流的 narrow
  const el = canvas;
  const g = ctx;
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  let w = 0;
  let h = 0;
  let rgb = { r: 56, g: 189, b: 248 };
  const mouse = { x: -9999, y: -9999 };
  let parts: { x: number; y: number; vx: number; vy: number }[] = [];

  // 从 --primary 读主题色（十六进制），MutationObserver 监听 data-theme 变化重读
  function readColor(): void {
    const s = getComputedStyle(document.documentElement).getPropertyValue('--primary').trim();
    const m = /^#?([0-9a-f]{6})$/i.exec(s);
    if (m) {
      const n = parseInt(m[1], 16);
      rgb = { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
    }
  }

  function resize(): void {
    w = el.clientWidth;
    h = el.clientHeight;
    el.width = Math.floor(w * dpr);
    el.height = Math.floor(h * dpr);
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    // 粒子数随视口面积缩放，上限兼顾观感与开销
    const n = Math.max(28, Math.min(80, Math.round((w * h) / 18000)));
    parts = Array.from({ length: n }, () => ({
      x: Math.random() * w,
      y: Math.random() * h,
      vx: (Math.random() - 0.5) * 0.35,
      vy: (Math.random() - 0.5) * 0.35,
    }));
  }

  function drawStatic(): void {
    g.clearRect(0, 0, w, h);
    for (const p of parts) {
      g.fillStyle = `rgba(${rgb.r},${rgb.g},${rgb.b},0.65)`;
      g.beginPath();
      g.arc(p.x, p.y, 1.6, 0, Math.PI * 2);
      g.fill();
    }
  }

  function frame(): void {
    g.clearRect(0, 0, w, h);
    const LINK = 130;
    const MOUSE = 170;
    for (const p of parts) {
      p.x += p.vx;
      p.y += p.vy;
      if (p.x < 0 || p.x > w) p.vx *= -1;
      if (p.y < 0 || p.y > h) p.vy *= -1;
    }
    for (let i = 0; i < parts.length; i++) {
      const a = parts[i];
      for (let j = i + 1; j < parts.length; j++) {
        const b = parts[j];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        if (d < LINK) {
          g.strokeStyle = `rgba(${rgb.r},${rgb.g},${rgb.b},${(1 - d / LINK) * 0.22})`;
          g.lineWidth = 1;
          g.beginPath();
          g.moveTo(a.x, a.y);
          g.lineTo(b.x, b.y);
          g.stroke();
        }
      }
      const md = Math.hypot(a.x - mouse.x, a.y - mouse.y);
      if (md < MOUSE) {
        g.strokeStyle = `rgba(${rgb.r},${rgb.g},${rgb.b},${(1 - md / MOUSE) * 0.45})`;
        g.lineWidth = 1;
        g.beginPath();
        g.moveTo(a.x, a.y);
        g.lineTo(mouse.x, mouse.y);
        g.stroke();
      }
    }
    for (const p of parts) {
      g.fillStyle = `rgba(${rgb.r},${rgb.g},${rgb.b},0.85)`;
      g.beginPath();
      g.arc(p.x, p.y, 1.6, 0, Math.PI * 2);
      g.fill();
    }
    fxRaf = requestAnimationFrame(frame);
  }

  const onMove = (e: MouseEvent): void => {
    const rect = el.getBoundingClientRect();
    mouse.x = e.clientX - rect.left;
    mouse.y = e.clientY - rect.top;
  };
  const onLeave = (): void => {
    mouse.x = -9999;
    mouse.y = -9999;
  };
  const onResize = (): void => {
    resize();
    if (reduce) drawStatic();
  };

  readColor();
  resize();
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseout', onLeave);
  window.addEventListener('resize', onResize);
  fxThemeObserver = new MutationObserver(readColor);
  fxThemeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme'],
  });

  if (reduce) drawStatic();
  else fxRaf = requestAnimationFrame(frame);

  fxCleanup = () => {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseout', onLeave);
    window.removeEventListener('resize', onResize);
  };
}

function stopFx(): void {
  if (fxRaf) cancelAnimationFrame(fxRaf);
  fxRaf = 0;
  if (fxThemeObserver) fxThemeObserver.disconnect();
  fxThemeObserver = null;
  if (fxCleanup) fxCleanup();
  fxCleanup = null;
}

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

function loadSide(): void {
  // 分类与标签都走共享 store（顶栏已拉取，这里只保证 Hero 统计有数），失败不阻塞正文
  loadCategories();
  loadTags();
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
  initFx();
});

onBeforeUnmount(stopFx);
</script>

<template>
  <div class="home">
    <!-- 动态背景：漂移渐变光斑 + 画布「星座」粒子（随鼠标连线），铺满视口且在内容之下 -->
    <div class="fx-backdrop" aria-hidden="true">
      <span class="fx-orb fx-orb--a"></span>
      <span class="fx-orb fx-orb--b"></span>
      <span class="fx-orb fx-orb--c"></span>
      <canvas ref="fxCanvas" class="fx-canvas"></canvas>
    </div>

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

    <!-- 区块标题：首页才有的「最新文章」导引行，让网格不再突然开始 -->
    <div v-if="route.name === 'home' && !filterBanner" class="grid-head">
      <h2 class="grid-head-title">最新文章</h2>
      <span class="grid-head-sub">共 {{ total }} 篇 · 支持 AI 语义检索</span>
    </div>

    <a-spin :spinning="loading">
      <!-- 封面卡片网格：一行 lg 3 / sm 2 / xs 1，封面置顶、正文在下 -->
      <a-row :gutter="[24, 24]">
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
  </div>
</template>

<style scoped>
/* --- Hero：横向信息区，左文右搜索面板 --- */
.home {
  position: relative;
  min-height: 240px;
}

/* 动态背景层：fixed 铺满顶栏以下的视口（top 避开半透明吸顶栏）；
   再用顶部遮罩把最上一段渐隐掉，避免光斑/粒子“贴”在顶栏下沿随鼠标动，像顶栏在跟着下来 */
.fx-backdrop {
  position: fixed;
  top: var(--header-h);
  right: 0;
  bottom: 0;
  left: 0;
  z-index: 0;
  overflow: hidden;
  pointer-events: none;
  -webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 140px);
  mask-image: linear-gradient(to bottom, transparent 0, #000 140px);
}

.home > :not(.fx-backdrop) {
  position: relative;
  z-index: 1;
}

.fx-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
}

.fx-orb {
  position: absolute;
  border-radius: 50%;
  filter: blur(64px);
  opacity: var(--fx-orb-opacity, 0.4);
  will-change: transform;
}

.fx-orb--a {
  width: 46vw;
  height: 46vw;
  top: -14vw;
  left: -10vw;
  background: radial-gradient(circle at 50% 50%, var(--accent), transparent 70%);
  animation: fx-drift-a 20s ease-in-out infinite;
}

.fx-orb--b {
  width: 42vw;
  height: 42vw;
  top: -8vw;
  right: -8vw;
  background: radial-gradient(circle at 50% 50%, var(--violet), transparent 70%);
  animation: fx-drift-b 26s ease-in-out infinite;
}

.fx-orb--c {
  width: 36vw;
  height: 36vw;
  bottom: -12vw;
  left: 32vw;
  background: radial-gradient(circle at 50% 50%, var(--primary), transparent 70%);
  animation: fx-drift-c 30s ease-in-out infinite;
}

@keyframes fx-drift-a {
  0%,
  100% {
    transform: translate(0, 0) scale(1);
  }
  50% {
    transform: translate(7vw, 6vh) scale(1.18);
  }
}

@keyframes fx-drift-b {
  0%,
  100% {
    transform: translate(0, 0) scale(1);
  }
  50% {
    transform: translate(-6vw, 8vh) scale(1.12);
  }
}

@keyframes fx-drift-c {
  0%,
  100% {
    transform: translate(0, 0) scale(1);
  }
  50% {
    transform: translate(5vw, -7vh) scale(1.22);
  }
}

/* 尊重系统「减弱动态效果」：光斑静止，画布侧在 initFx 里只画静态点 */
@media (prefers-reduced-motion: reduce) {
  .fx-orb {
    animation: none !important;
  }
}

.hero {
  display: flex;
  align-items: stretch;
  gap: 40px;
  padding: 48px 0 24px;
}

.hero-text {
  flex: 1;
  min-width: 0;
}

.hero-kicker {
  margin: 0;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 12.5px;
  letter-spacing: 4px;
  color: var(--primary);
  opacity: 0.9;
}

.hero-title {
  margin: 16px 0 14px;
  font-size: clamp(32px, 5.2vw, 54px);
  font-weight: 800;
  letter-spacing: 0.5px;
  line-height: 1.18;
  background: var(--grad);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.hero-desc {
  margin: 0;
  max-width: 560px;
  color: var(--text-soft);
  font-size: 16px;
  line-height: 1.75;
}

.hero-stats {
  display: flex;
  gap: 16px;
  margin-top: 30px;
}

.hero-stat {
  min-width: 104px;
  padding: 14px 20px;
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
  font-size: 30px;
  font-weight: 700;
  line-height: 1.15;
  background: var(--grad);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.hero-label {
  font-size: 13px;
  color: var(--text-soft);
}

/* Hero 右侧：AI 搜索入口面板 */
.hero-search {
  width: 360px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 14px;
  padding: 28px;
  background: var(--bg-card);
  backdrop-filter: var(--card-glass);
  -webkit-backdrop-filter: var(--card-glass);
  border: 1px solid var(--border);
  border-radius: 20px;
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

/* --- 区块导引行：让文章网格有一个明确的起点 --- */
.grid-head {
  display: flex;
  align-items: baseline;
  gap: 14px;
  margin: 20px 0 22px;
}

.grid-head-title {
  position: relative;
  margin: 0;
  padding-left: 16px;
  font-size: 24px;
  font-weight: 800;
  letter-spacing: 0.5px;
}

.grid-head-title::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0.14em;
  bottom: 0.14em;
  width: 5px;
  border-radius: 5px;
  background: var(--grad);
}

.grid-head-sub {
  color: var(--text-soft);
  font-size: 13.5px;
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
