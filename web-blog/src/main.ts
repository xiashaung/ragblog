import { createApp } from 'vue';
import Antd from 'ant-design-vue';
import 'ant-design-vue/dist/reset.css';
import App from './App.vue';
import router from './router';
import './styles/global.css';
import { initTheme } from './composables/useTheme';

// 主题要在挂载前落在 <html> 上，避免暗色用户刷新时闪一下白
initTheme();

createApp(App).use(router).use(Antd).mount('#app');
