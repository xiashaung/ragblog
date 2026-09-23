import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import vueJsx from '@vitejs/plugin-vue-jsx';

// Vben 规范化: @ -> src, 统一别名; 后端 /api 经 proxy 转发 8011
// base=/admin/：控制台挂站点 /admin 路径下（与后端 app/main.py 的 SPA 托管对应），
// 管理 API 已收进 /api/v1/admin，dev 代理只需转发 /api 与 /media。
export default defineConfig({
  base: '/admin/',
  plugins: [vue(), vueJsx()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    host: '127.0.0.1',
    proxy: {
      // 把 /api 经 proxy 转发到 FastAPI 后端(8011)，含 /api/v1/admin 管理 API
      '/api': {
        target: 'http://127.0.0.1:8011',
        changeOrigin: true,
      },
      // 上传的图片经 /media 静态服务，dev 下同样转发到后端
      '/media': {
        target: 'http://127.0.0.1:8011',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: '../web/dist', // 关键: 产物直接打入 8011 SPA 挂载目录 (fastapi/web/dist, within session workspace)
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
  optimizeDeps: { include: ['vue', 'vue-router', 'pinia', 'ant-design-vue'] },
});
