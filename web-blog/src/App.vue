<script setup lang="ts">
// 应用外壳：顶栏（站名 / 导航 / 主题切换）+ 内容区 + 页脚。
// 分类导航收进顶栏下拉：分类是全站维度入口，不该只待在首页侧栏里。
// antd 组件（输入框/按钮/tooltip 等）的明暗算法靠 ConfigProvider 与 data-theme 联动，
// 否则暗色下仍是白底黑字：搜索区不可读、文字按钮看不见。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { theme } from 'ant-design-vue';
import {
  AppstoreOutlined,
  BulbFilled,
  BulbOutlined,
  CaretDownOutlined,
  HomeOutlined,
  RobotOutlined,
  VerticalAlignTopOutlined,
} from '@ant-design/icons-vue';
import { isDark, toggleTheme } from './composables/useTheme';
import { categories, loadCategories } from './stores/categories';

const route = useRoute();
const router = useRouter();

// 主色与 global.css 的 --primary 两套主题各自对齐，组件与页面色调统一；
// 暗色时把 antd 容器/弹层/布局底色也拉到本站「深空蓝」系，输入框不发灰
const antdTheme = computed(() =>
  isDark.value
    ? {
        algorithm: theme.darkAlgorithm,
        token: {
          colorPrimary: '#38bdf8',
          colorBgElevated: '#101827',
          colorBgContainer: '#0f1728',
          colorBgLayout: '#070b14',
        },
      }
    : { algorithm: theme.defaultAlgorithm, token: { colorPrimary: '#2563eb' } },
);

// 页脚版权年份动态取，免得每年手改一次
const year = new Date().getFullYear();

// 回到顶部悬浮按钮：滚过约一屏的三分之一才现身
const showBackTop = ref(false);

// 当前正被顶栏「分类」下拉选中的分类 slug（高亮下拉入口用）
const activeCategorySlug = computed(() =>
  route.name === 'category' && typeof route.params.slug === 'string' ? route.params.slug : '',
);

// 分类下拉：走 #overlay + a-menu 的写法（与 ThemeToggle/MainLayout 一致）；
// antd-vue 的 Dropdown 对 :menu prop 对象里的 onClick 支持不可靠，点了不触发跳转
function goCategory(slug: string): void {
  router.push({ name: 'category', params: { slug } });
}

function onCatMenuClick({ key }: { key: string | number }): void {
  goCategory(String(key));
}

function onWindowScroll(): void {
  showBackTop.value = window.scrollY > window.innerHeight / 3;
}

function backToTop(): void {
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

onMounted(() => {
  window.addEventListener('scroll', onWindowScroll, { passive: true });
  loadCategories();
});
onBeforeUnmount(() => window.removeEventListener('scroll', onWindowScroll));
</script>

<template>
  <a-config-provider :theme="antdTheme">
    <div class="blog-shell">
      <header class="blog-header">
        <div class="blog-header-inner">
          <router-link to="/" class="blog-logo">我的博客</router-link>
          <nav class="blog-nav">
            <router-link to="/" class="nav-link">
              <HomeOutlined /> 首页
            </router-link>
            <!-- 分类下拉：超过顶栏宽度时 antd 自己出滚动，不拆行 -->
            <a-dropdown :trigger="['hover']" placement="bottomLeft">
              <span
                class="nav-link nav-link-dropdown"
                :class="{ 'nav-link-active': activeCategorySlug }"
              >
                <AppstoreOutlined /> 分类 <CaretDownOutlined class="nav-caret" />
              </span>
              <template #overlay>
                <a-menu
                  :selected-keys="activeCategorySlug ? [activeCategorySlug] : []"
                  @click="onCatMenuClick"
                >
                  <a-menu-item v-for="c in categories" :key="c.slug">
                    {{ c.name }}（{{ c.post_count }}）
                  </a-menu-item>
                </a-menu>
              </template>
            </a-dropdown>
            <router-link to="/search" class="nav-link">
              <RobotOutlined /> AI 搜索
            </router-link>
          </nav>
          <a-tooltip :title="isDark ? '切换到亮色' : '切换到暗色'">
            <a-button type="text" @click="toggleTheme">
              <BulbFilled v-if="isDark" />
              <BulbOutlined v-else />
            </a-button>
          </a-tooltip>
        </div>
      </header>

      <main class="blog-main">
        <router-view />
      </main>

      <!-- 页脚：上段品牌 + 快捷导航，下段版权 / 备案，与整站渐变语言统一 -->
      <footer class="blog-footer">
        <div class="blog-footer-inner">
          <div class="footer-brand">
            <span class="blog-logo">我的博客</span>
            <p class="footer-slogan">记录每一次技术探索 · 支持自然语言检索与 AI 追问</p>
          </div>
          <nav class="footer-links">
            <router-link to="/">首页</router-link>
            <router-link to="/search">AI 搜索</router-link>
          </nav>
        </div>
        <div class="footer-bottom">
          <span>© {{ year }} 我的博客 · Powered by FastAPI + Vue 3</span>
          <a class="footer-icp" target="_blank" href="https://beian.miit.gov.cn/#/Integrated/index">苏ICP备2021016119号-1</a>
        </div>
      </footer>

      <transition name="fade-up">
        <a-button
          v-show="showBackTop"
          class="back-top"
          shape="circle"
          size="large"
          @click="backToTop"
        >
          <template #icon><VerticalAlignTopOutlined /></template>
        </a-button>
      </transition>
    </div>
  </a-config-provider>
</template>
