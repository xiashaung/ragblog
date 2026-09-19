/** 应用入口 —— 注册 Ant Design Vue、Pinia、Vue Router，挂载根组件。 */
import { createApp } from 'vue';
import { pinia } from './store';
import Antd from 'ant-design-vue';
import 'ant-design-vue/dist/reset.css';
import './styles/global.css';

import App from './App.vue';
import router from './router';
import { Modal } from 'ant-design-vue';

const app = createApp(App);

// 把 antd modal 挂载到 window，供 401 守卫等无组件场景调用
(globalThis as any).modal = Modal;

app.use(pinia);
app.use(router);
app.use(Antd);

app.mount('#app');
