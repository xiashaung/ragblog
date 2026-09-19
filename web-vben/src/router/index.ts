/**
 * 路由定义 —— 父级套子级：一个「菜单分组」就是一个父路由，页面是它的子路由。
 *
 * 两个刻意的约定：
 * 1. 分组父路由用组自己的 slug（/ai、/rag、/data、/system），且**不与任何页面 URL 重名**，
 *    各自 redirect 到组内第一个页面，所以直接访问 /rag 也会落到「统计概览」。
 * 2. 子路由写**绝对路径**（vue-router 4 支持：层级来自嵌套而不是路径前缀），因此引入
 *    嵌套不改动任何既有 URL；层级只体现在 route.matched 上，供侧栏菜单与面包屑直接推导
 *    ——不再需要 meta.group 这种「假分组」标记，也不必在布局里维护第二份分组对照表。
 *
 * 守卫：未登录跳登录页；权限不足跳 403（权限码与后端 require_permission 同一套）。
 */
import { defineComponent, h } from 'vue';
import { createRouter, createWebHistory, RouterView, type RouteRecordRaw } from 'vue-router';
import { useAuthStore } from '@/store/modules/auth';

/** 分组层透传组件：自身无内容，只开一个子路由出口 */
const RouteGroup = defineComponent({
  name: 'RouteGroup',
  inheritAttrs: false,
  render: () => h(RouterView),
});

const routes: RouteRecordRaw[] = [
  // 登录页（RBAC 后只负责登录，建账号统一走首次引导页）
  {
    path: '/auth/login',
    name: 'login',
    component: () => import('@/views/auth/LoginPage.vue'),
    meta: { title: '登录', needAuth: false, public: true },
  },
  // 首次引导：创建唯一一个超级管理员（需 APP_ADMIN_API_KEY）
  {
    path: '/auth/onboard',
    name: 'onboard',
    component: () => import('@/views/auth/OnboardPage.vue'),
    meta: { title: '首次引导', public: true },
  },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    redirect: '/dashboard',
    children: [
      // 顶级单项：不归属任何分组，直接作为侧栏第一项
      {
        path: '/dashboard',
        name: 'dashboard',
        component: () => import('@/views/_core/Dashboard.vue'),
        meta: { title: '概览', needAuth: true, icon: 'DashboardOutlined' },
      },
      {
        path: '/ai',
        component: RouteGroup,
        redirect: '/chat',
        meta: { title: 'AI 能力', icon: 'ThunderboltOutlined' },
        children: [
          {
            path: '/chat',
            name: 'chat',
            component: () => import('@/views/ai/ChatPage.vue'),
            meta: { title: 'AI 对话', needAuth: true, icon: 'MessageOutlined' },
          },
          {
            path: '/embed',
            name: 'embed',
            component: () => import('@/views/ai/EmbedPage.vue'),
            meta: { title: '嵌入向量', needAuth: true, icon: 'CodeOutlined' },
          },
        ],
      },
      {
        path: '/rag',
        component: RouteGroup,
        redirect: '/knowledge/overview',
        meta: { title: '知识库', icon: 'DatabaseOutlined' },
        children: [
          {
            path: '/knowledge/overview',
            name: 'knowledge-overview',
            component: () => import('@/views/rag/KnowledgeOverviewPage.vue'),
            meta: { title: '统计概览', needAuth: true, icon: 'FundOutlined' },
          },
          {
            path: '/knowledge',
            name: 'knowledge-docs',
            component: () => import('@/views/rag/KnowledgeDocsPage.vue'),
            meta: { title: '文档列表', needAuth: true, icon: 'FileTextOutlined' },
          },
          {
            path: '/knowledge/ingest',
            name: 'knowledge-ingest',
            component: () => import('@/views/rag/KnowledgeIngestPage.vue'),
            meta: { title: '内容录入', needAuth: true, icon: 'CloudUploadOutlined' },
          },
          {
            path: '/knowledge/search',
            name: 'knowledge-search',
            component: () => import('@/views/rag/KnowledgeSearchPage.vue'),
            meta: { title: '检索问答', needAuth: true, icon: 'SearchOutlined' },
          },
        ],
      },
      {
        path: '/data',
        component: RouteGroup,
        redirect: '/analytics',
        meta: { title: '数据分析', icon: 'BarChartOutlined' },
        children: [
          {
            path: '/analytics',
            name: 'analytics',
            component: () => import('@/views/analytics/AnalyticsPage.vue'),
            meta: {
              title: '智能问数',
              needAuth: true,
              permissions: ['analytics:read'],
              icon: 'BarChartOutlined',
            },
          },
          {
            path: '/datasources',
            name: 'datasources',
            component: () => import('@/views/admin/DataSourcesPage.vue'),
            meta: {
              title: '数据源管理',
              needAuth: true,
              permissions: ['datasource:read'],
              icon: 'DatabaseOutlined',
            },
          },
        ],
      },
      {
        path: '/system',
        component: RouteGroup,
        redirect: '/sessions',
        meta: { title: '系统管理', icon: 'SettingOutlined' },
        children: [
          {
            path: '/sessions',
            name: 'sessions',
            component: () => import('@/views/_core/SessionsPage.vue'),
            meta: {
              title: '会话',
              needAuth: true,
              permissions: ['session:read'],
              icon: 'SolutionOutlined',
            },
          },
          {
            path: '/accounts',
            name: 'accounts',
            component: () => import('@/views/admin/AccountsPage.vue'),
            meta: {
              title: '账号管理',
              needAuth: true,
              permissions: ['account:read'],
              icon: 'TeamOutlined',
            },
          },
          {
            path: '/roles',
            name: 'roles',
            component: () => import('@/views/admin/RolesPage.vue'),
            meta: {
              title: '角色权限',
              needAuth: true,
              permissions: ['role:read'],
              icon: 'SafetyCertificateOutlined',
            },
          },
        ],
      },
    ],
  },
  {
    path: '/403',
    name: 'forbidden',
    component: () => import('@/views/_core/Forbidden.vue'),
    meta: { title: '无访问权限', public: true },
  },
  { path: '/:pathMatch(.*)*', component: () => import('@/views/_core/NotFound.vue') },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

// 路由守卫：白名单放行 → 登录态 → 权限码
router.beforeEach(async (to) => {
  if (to.meta.public) return true;
  if (!localStorage.getItem('admin_token')) {
    return { path: '/auth/login', query: { redirect: to.fullPath } };
  }
  const auth = useAuthStore();
  // 刷新页面后 localStorage 里的权限可能为空（老版本登录过）或缺失：
  // 以服务端 /admin/me 为准补一次，避免菜单与路由判定不一致。
  if (!auth.permissions?.length) {
    try {
      await auth.fetchMe();
    } catch {
      /* 401 由请求拦截器统一处理（清 token + 跳登录） */
    }
  }
  const needed = (to.meta.permissions as string[] | undefined) ?? [];
  if (needed.length && !auth.hasPerm(needed)) {
    return { name: 'forbidden', query: { from: to.fullPath } };
  }
  return true;
});

router.afterEach((to) => {
  // 更新页面 title（可选）
  const metaTitle = to.meta?.title;
  if (metaTitle) document.title = `${metaTitle} - FastAPI 管理台`;
});

export default router;
