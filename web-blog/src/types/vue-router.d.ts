// vue-router 的 RouteMeta 增强。
// 注意：这个文件必须本身是「模块」（有顶层 import），否则 declare module
// 会变成环境模块声明、整体覆盖 vue-router 的真实类型（通配 *.vue 声明同理
// 必须在脚本文件里，所以两边不能合并在同一个文件）。
import 'vue-router';

declare module 'vue-router' {
  interface RouteMeta {
    /** 页面标题（浏览器标签页） */
    title?: string;
  }
}
