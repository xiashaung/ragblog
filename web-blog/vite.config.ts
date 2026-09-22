import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// 博客前台：base 必须是 /blog/（后端把产物挂在同一路径下），
// 路由 createWebHistory('/blog/') 与之配套；dev 端口 5174，与后台（5173）并行。
export default defineConfig({
  base: '/blog/',
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5174,
    host: '127.0.0.1',
    proxy: {
      // 公开接口与上传的图片都转发到后端（8011）
      '/api': { target: 'http://127.0.0.1:8011', changeOrigin: true },
      '/media': { target: 'http://127.0.0.1:8011', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
  },
});
