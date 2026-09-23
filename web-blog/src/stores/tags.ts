// 标签清单的共享状态：顶栏导航的「标签」下拉与首页 Hero 统计都要用，只拉一次。
// 与分类同一套模块级 ref 写法：避免同一接口被各组件各调一次，也保证高亮态一致。
import { ref } from 'vue';
import { blogApi, type TagItem } from '@/api';

export const tags = ref<TagItem[]>([]);

let loaded = false;

/** 拉标签清单；失败保持为空数组（下拉只剩占位），不阻塞页面；成功后只拉一次。 */
export async function loadTags(): Promise<void> {
  if (loaded) return;
  try {
    const res = await blogApi.tags();
    tags.value = res.items;
    loaded = true;
  } catch {
    /* 静默：标签拉不到时顶栏下拉为空，不影响其他导航 */
  }
}
