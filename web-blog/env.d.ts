/// <reference types="vite/client" />
declare module '*.vue' {
  import type { DefineComponent } from 'vue';
  const component: DefineComponent<{}, {}, any>;
  export default component;
}
// 注意：本文件是脚本（无顶层 import），只能放环境模块声明；
// vue-router 的 RouteMeta 增强在 src/types/vue-router.d.ts（需要是模块）。
