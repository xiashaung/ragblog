/** 应用入口 —— 注册 Ant Design Vue、Pinia、Vue Router 与权限指令，挂载根组件。 */
import { createApp } from 'vue';
import { pinia } from './store';
import Antd from 'ant-design-vue';
import 'ant-design-vue/dist/reset.css';
import './styles/global.css';

import App from './App.vue';
import router from './router';
import permDirectives from './directives/perm';
import { Modal } from 'ant-design-vue';

const app = createApp(App);

// 把 antd modal 挂载到 window，供 401 守卫等无组件场景调用
(globalThis as any).modal = Modal;

app.use(pinia);
app.use(router);
app.use(Antd);
// 按钮级权限：v-perm / v-perm-any（要在 pinia 之后注册，指令运行时要读 auth store）
app.use(permDirectives);

app.mount('#app');
