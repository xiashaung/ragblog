// 分类清单的共享状态：顶栏导航的下拉与首页侧栏/Hero 都要用，只拉一次。
// 放在模块级 ref 而不是各组件各拉一份：避免同一接口被调两次，也保证高亮态一致。
import { ref } from 'vue';
import { blogApi, type CategoryItem } from '@/api';

export const categories = ref<CategoryItem[]>([]);

let loaded = false;

/** 拉分类清单；失败保持为空数组（下拉只剩占位），不阻塞页面；成功后只拉一次。 */
export async function loadCategories(): Promise<void> {
  if (loaded) return;
  try {
    const res = await blogApi.categories();
    categories.value = res.items;
    loaded = true;
  } catch {
    /* 静默：分类拉不到时顶栏下拉为空，不影响其他导航 */
  }
}
