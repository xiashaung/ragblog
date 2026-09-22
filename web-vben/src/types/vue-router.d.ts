// vue-router 的 RouteMeta 增强（与 src/router/index.ts 里的定义保持一致）。
// 注意：这个文件必须本身是「模块」（有顶层 import），否则 declare module
// 会变成环境模块声明、整体覆盖 vue-router 的真实类型。
import 'vue-router';

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
