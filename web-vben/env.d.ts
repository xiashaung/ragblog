/// <reference types="vite/client" />
declare module '*.vue' {
  import type { DefineComponent } from 'vue';
  const component: DefineComponent<{}, {}, any>;
  export default component;
}

// 路由 meta 字段（与 src/router/index.ts 里的定义保持一致）
declare module 'vue-router' {
  interface RouteMeta {
    title?: string;
    needAuth?: boolean;
    public?: boolean;
    icon?: string;
    /** RBAC：进入该页所需的权限码 */
    permissions?: string[];
  }
}
