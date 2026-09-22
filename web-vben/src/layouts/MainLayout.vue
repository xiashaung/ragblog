<script setup lang="ts">
/**
 * 主布局 —— Vben 三件套：左侧菜单 + 顶栏（面包屑/主题/身份/健康度）+ 内容区(KeepAlive 路由)。
 * 侧边菜单、面包屑、分组展开态全部从路由树（父级套子级）递归推导，权限码不足的项直接不展示。
 */
import { computed, h, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useAuthStore } from '@/store/modules/auth';
import { useBusinessStore } from '@/store/modules/business';
import { useThemeStore } from '@/store/modules/theme';
import { Modal } from 'ant-design-vue';
import type { RouteRecordRaw } from 'vue-router';
import ThemeToggle from '@/components/ThemeToggle.vue';
import {
  DownOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  // 图标组件：用于从 meta.icon 名称映射
  DashboardOutlined,
  MessageOutlined,
  ReadOutlined,
  FileTextOutlined,
  CloudUploadOutlined,
  FundOutlined,
  SearchOutlined,
  TeamOutlined,
  SolutionOutlined,
  SafetyCertificateOutlined,
  BarChartOutlined,
  SlidersOutlined,
  // 分组标题与叶子页面共用这一份映射（名字取自 router 里的 meta.icon）
  ThunderboltOutlined,
  DatabaseOutlined,
  SettingOutlined,
  // 博客分组：分组封面、文章管理、分类管理
  FileMarkdownOutlined,
  FormOutlined,
  FolderOutlined,
} from '@ant-design/icons-vue';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const business = useBusinessStore();
const theme = useThemeStore();

const collapsed = ref(false);
const healthTimer = ref<any>(null);
const activeMenu = ref(route.path);

// ── 图标名称 → 组件 映射表 ──
const iconMap: Record<string, unknown> = {
  DashboardOutlined,
  MessageOutlined,
  ReadOutlined,
  FileTextOutlined,
  CloudUploadOutlined,
  FundOutlined,
  SearchOutlined,
  TeamOutlined,
  SolutionOutlined,
  SafetyCertificateOutlined,
  BarChartOutlined,
  SlidersOutlined,
  ThunderboltOutlined,
  DatabaseOutlined,
  SettingOutlined,
  FileMarkdownOutlined,
  FormOutlined,
  FolderOutlined,
};

// ── 菜单与面包屑都从路由树本身推导（父级套子级），不再在布局里维护第二份分组表 ──

// 面包屑：首页（概览）+ route.matched 上各级 meta.title，天然就是「分组 / 页面」全链路。
// 分组层只是透传层级、不是可停留的页面，所以除首页外一律渲染为纯文本。
const breadcrumbs = computed(() => {
  const items: { title: string; path?: string }[] =
    route.path === '/dashboard' ? [] : [{ title: '概览', path: '/dashboard' }];
  route.matched.forEach((m) => {
    const title = m.meta?.title as string | undefined;
    if (title) items.push({ title });
  });
  return items;
});

// 只展开「当前页所在的那一层分组」：matched 掐头（布局）去尾（当前页）就是分组链，
// 取最深一层当 openKeys；顶级项（概览）没有分组 → 收起全部。
const activeGroupKey = computed(() => {
  const groups = route.matched.slice(1, -1);
  return groups.length ? String(groups[groups.length - 1].path) : '';
});
const openKeys = ref<string[]>(activeGroupKey.value ? [activeGroupKey.value] : []);
watch(activeGroupKey, (key) => {
  // 顶级项（如概览，无分组）时清空，收起全部分组；否则仅保留当前分组。
  openKeys.value = key ? [key] : [];
});

// 子路由写的是绝对路径（见 router/index.ts 注释），所以菜单 key 直接就是可跳转 URL
const pathKey = (p: string) => (p.startsWith('/') ? p : `/${p}`);

interface MenuNode {
  key: string;
  label: string;
  icon?: () => unknown;
  permissions: string[];
  children?: MenuNode[];
}

/**
 * 路由记录 → 菜单节点：有 children 的是分组（可展开），没有的是页面（可点击）。
 * icon 用函数式（antd items 推荐写法），避免把预创建 VNode 塞进被深度监听的数组里。
 * 权限不足的项直接不展示（后端仍会独立拦截，这里只是避免点了才 403）；分组下
 * 可见叶子被过滤光时整组收起。
 */
function toMenuItem(r: RouteRecordRaw): MenuNode | null {
  const key = pathKey(r.path);
  const iconKey = (r.meta?.icon as string) || undefined;
  const IconComp = iconKey ? (iconMap[iconKey] as typeof DashboardOutlined) : undefined;
  const node: MenuNode = {
    key,
    label: (r.meta?.title as string) || String(r.name || key),
    icon: IconComp ? () => h(IconComp) : undefined,
    permissions: (r.meta?.permissions as string[] | undefined) ?? [],
  };
  if (!auth.hasPerm(node.permissions)) return null;
  if (r.children?.length) {
    const kids = r.children
      .map(toMenuItem)
      .filter((x): x is MenuNode => x !== null);
    return kids.length ? { ...node, children: kids } : null;
  }
  return node;
}

// ── 侧栏菜单：从布局路由的 children 递归生成，顺序即路由声明顺序 ──
const menuItems = computed(() => {
  const root = router.options.routes.find(
    (r) => typeof r.path === 'string' && r.path === '/'
  );
  return (root?.children || [])
    .map(toMenuItem)
    .filter((x): x is MenuNode => x !== null);
});

function onCollapse(collapsedVal: boolean) {
  collapsed.value = collapsedVal;
}

function toPage(path: string) {
  activeMenu.value = path;
  router.push(path);
}

function handleLogout() {
  Modal.confirm({
    title: '确认登出',
    content: '将清除当前管理会话（admin_token）',
    onOk: async () => {
      try {
        await auth.logout();
      } catch {
        /* ignore */
      }
      auth.logout();
      router.push('/auth/login');
    },
  });
}

onMounted(async () => {
  // 未登录时不调用任何 API，避免 401 错误
  if (!auth.token.value) return;
  // 先以服务端为准刷新权限（角色可能在别处被改过），再拉业务数据
  try {
    await auth.fetchMe();
  } catch {
    /* 拦截器会处理 401 */
  }
  business.loadProviders();
  business.pollHealth();
  // 会话计数需要 session:read：无权就不发这个请求（否则每次进页都吃一个 403）
  if (auth.hasPerm('session:read')) business.loadSessions();
  // 30s 轮询健康
  healthTimer.value = setInterval(() => business.pollHealth(), 30_000);
});

onUnmounted(() => {
  if (healthTimer.value) clearInterval(healthTimer.value);
});
</script>

<template>
  <a-layout class="vben-layout">
    <!-- 左侧菜单 -->
    <a-layout-sider
      v-model:collapsed="collapsed"
      width="240"
      collapsible
      class="vben-sider"
      @collapse="onCollapse"
    >
      <div class="brand">{{ collapsed ? '⚡' : '⚡ FastAPI 控制台' }}</div>
      <!-- 菜单自身明暗也绑定主题：折叠态弹出的子菜单在组件树外渲染，只能靠 antd 自己的 dark 算法配色 -->
      <a-menu
        mode="inline"
        :theme="theme.isDark ? 'dark' : 'light'"
        :selected-keys="[route.path]"
        v-model:open-keys="openKeys"
        :items="menuItems"
        @select="({ key }) => toPage(key)"
      />
    </a-layout-sider>

    <a-layout>
      <!-- 顶栏 -->
      <a-layout-header class="vben-header">
        <div class="header-left">
          <a-tooltip :title="`当前地址：${route.fullPath}`">
            <a-breadcrumb>
              <a-breadcrumb-item v-for="(item, i) in breadcrumbs" :key="`${item.title}-${i}`">
                <a v-if="item.path" @click="toPage(item.path)">{{ item.title }}</a>
                <span v-else>{{ item.title }}</span>
              </a-breadcrumb-item>
            </a-breadcrumb>
          </a-tooltip>
        </div>
        <div class="header-right">
          <ThemeToggle />

          <a-tooltip
            :title="
              business.backendOk === false
                ? '后端未响应（/health）'
                : '后端正常'
            "
          >
            <a-badge
              :status="
                business.backendOk
                  ? 'success'
                  : business.backendOk === false
                    ? 'error'
                    : 'default'
              "
              text="后端"
            />
          </a-tooltip>

          <a-tooltip :title="`生效角色：${auth.roles.join('、') || '无'}`">
            <a-tag :color="auth.isSuperadmin ? 'gold' : 'purple'">
              {{ auth.roles[0] || '成员' }}
            </a-tag>
          </a-tooltip>

          <!-- 会话数本身就是一种只读数据，无权时整块隐藏（摆个「0 会话」只会让人以为真没人在线） -->
          <a-tooltip v-if="auth.hasPerm('session:read')" title="在线 admin 会话数">
            <a-badge count="0" :badge-offset="[4, 0]">
              <a-button type="text" @click="business.loadSessions()">
                {{ business.sessions.length }} 会话
              </a-button>
            </a-badge>
          </a-tooltip>

          <a-dropdown>
            <a-button type="text">
              {{ auth.userInfo?.name || auth.userInfo?.username || '管理员' }}
              <DownOutlined />
            </a-button>
            <template #overlay>
              <a-menu @click="handleLogout">
                <a-menu-item>登出</a-menu-item>
              </a-menu>
            </template>
          </a-dropdown>
        </div>
      </a-layout-header>

      <!-- 内容区：路由 + KeepAlive -->
      <a-layout-content class="vben-content">
        <router-view v-slot="{ Component }">
          <keep-alive>
            <component :is="Component" :key="route.fullPath" />
          </keep-alive>
        </router-view>
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>

<style scoped>
.vben-layout { height: 100vh; }
.vben-sider {
  background: var(--app-sider-bg);
  border-right: 1px solid var(--app-border);
}
.brand {
  height: 64px;
  line-height: 64px;
  text-align: center;
  color: var(--app-sider-brand-fg);
  font-size: 16px;
  font-weight: 600;
  background: var(--app-sider-brand-bg);
  border-bottom: 1px solid var(--app-border);
}
/* 菜单底透明，让侧栏底色透出，避免两层配色不同步 */
:deep(.ant-menu) {
  background: transparent;
}
:deep(.ant-menu-item),
:deep(.ant-menu-submenu-title) {
  color: var(--app-sider-fg);
}
/* 悬停 */
:deep(.ant-menu-item:hover),
:deep(.ant-menu-submenu-title:hover) {
  background-color: var(--app-sider-hover-bg) !important;
  color: var(--app-sider-hover-fg) !important;
}
:deep(.ant-menu-item:hover .ant-menu-item-icon),
:deep(.ant-menu-submenu-title:hover .ant-menu-item-icon) {
  color: var(--app-sider-hover-fg);
}
/* 选中项：品牌蓝底 + 白字，两种模式下对比度都够 */
:deep(.ant-menu-item-selected) {
  background-color: var(--app-sider-active-bg) !important;
  color: var(--app-sider-active-fg) !important;
}
:deep(.ant-menu-item-selected .ant-menu-item-icon) {
  color: var(--app-sider-active-fg);
}
/* 选中项被悬停时不该掉回灰底（:hover 与 -selected 特异性相同，靠源码顺序压住） */
:deep(.ant-menu-item-selected:hover) {
  background-color: var(--app-sider-active-bg) !important;
  color: var(--app-sider-active-fg) !important;
}
/* 含选中项的分组标题：只变文字色，不整块涂蓝，靠它提示当前所在分组 */
:deep(.ant-menu-submenu-selected > .ant-menu-submenu-title),
:deep(.ant-menu-submenu-selected > .ant-menu-submenu-title .ant-menu-item-icon) {
  color: var(--app-sider-group-active-fg);
}
/* 展开的子层用另一档底色，与父层拉开层次 */
:deep(.ant-menu-sub) {
  background: var(--app-sider-sub-bg) !important;
}
:deep(.ant-menu-sub .ant-menu-item) {
  background-color: var(--app-sider-sub-bg);
}
/* 折叠按钮条与侧栏同色（antd 默认取 colorBgContainer，会跟侧栏变量对不上） */
:deep(.ant-layout-sider-trigger) {
  background: var(--app-sider-bg);
  color: var(--app-sider-fg);
  border-top: 1px solid var(--app-border);
}
.vben-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--app-panel);
  border-bottom: 1px solid var(--app-border);
  padding: 0 24px;
}
.header-left { display: flex; align-items: center; }
.header-right { display: flex; align-items: center; gap: 16px; }
.vben-content {
  padding: 20px 24px;
  background: transparent;
  overflow: auto;
}
</style>
