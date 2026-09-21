/**
 * usePerm —— 脚本里读权限的组合函数（配 v-perm 指令一起用）。
 *
 * 分工：模板里的按钮优先用 v-perm（不用自己写 :disabled 与提示）；
 * 这里负责指令管不到的两类场景：
 *   1. 脚本里的判断 —— 没权限就别发那个请求（否则只收到一个 403，页面还白转圈）；
 *   2. 结构复杂的操作位 —— 比如表格行里的 <a>、下拉菜单项、上传组件，
 *      得自己把 :disabled 与 tooltip 拼上去，用 can() / tip() 配对。
 *
 * 后端才是唯一权威：这里的判断只影响交互观感，绕过去也过不了 require_permission。
 */
import { useAuthStore } from '@/store/modules/auth';
import { permNeed, permTip } from '@/config/permissions';

export function usePerm() {
  const auth = useAuthStore();

  /** 要求全部命中（数组即「&&」），与后端 require_permission 的默认口径一致 */
  const can = (code: string | string[]) => auth.hasPerm(code);
  /** 任一命中即可 */
  const canAny = (codes: string[]) => auth.hasAnyPerm(codes);
  /** 无权提示文案，配 :disabled 一起用在自定义结构上 */
  const tip = (code: string | string[], anyMode = false) =>
    permTip(Array.isArray(code) ? code : [code], anyMode);
  /** 只报「缺哪条权限」，给 toast / 正文用（不含「已禁用」那半句） */
  const need = (code: string | string[], anyMode = false) =>
    permNeed(Array.isArray(code) ? code : [code], anyMode);

  return { auth, can, canAny, tip, need };
}
