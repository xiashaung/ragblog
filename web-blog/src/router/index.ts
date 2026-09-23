import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router';

// 博客前台挂站点根路径 /（vite base=/），history base 取 BASE_URL 与之保持一致
const routes: RouteRecordRaw[] = [
  { path: '/', name: 'home', component: () => import('@/views/HomePage.vue'), meta: { title: '文章' } },
  { path: '/tag/:tag', name: 'tag', component: () => import('@/views/HomePage.vue'), meta: { title: '标签' } },
  { path: '/category/:slug', name: 'category', component: () => import('@/views/HomePage.vue'), meta: { title: '分类' } },
  { path: '/post/:slug', name: 'post', component: () => import('@/views/PostPage.vue'), meta: { title: '正文' } },
  { path: '/search', name: 'search', component: () => import('@/views/SearchPage.vue'), meta: { title: 'AI 搜索' } },
  { path: '/:pathMatch(.*)*', name: 'not-found', component: () => import('@/views/NotFound.vue'), meta: { title: '404' } },
];

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
});

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} - 个人博客` : '个人博客';
  // 导航成功即重置重载标记，为下一次版本更新后的 chunk 失效留机会
  sessionStorage.removeItem('chunk-reloaded');
});

// 发新版后旧 chunk 文件被替换，停留在旧页面的用户点路由时懒加载会失败，
// 表现为「点击无反应」：这种错误自动重载一次拉最新 index.html（防无限循环靠标记）。
router.onError((err) => {
  const msg = String((err as Error)?.message ?? err);
  if (
    /Failed to fetch dynamically imported module|Error loading dynamically imported module|Importing a module script failed/i.test(
      msg,
    ) &&
    !sessionStorage.getItem('chunk-reloaded')
  ) {
    sessionStorage.setItem('chunk-reloaded', '1');
    window.location.reload();
  }
});

export default router;
