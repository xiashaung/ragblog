/**
 * usePerm —— 脚本里读权限的组合函数（配 v-perm 指令一起用）。
 *
 * 分工：模板里的按钮优先用 v-perm（不用自己写 :disabled 与提示）；
 * 这里负责指令管不到的三类场景：
 *   1. 脚本里的判断 —— 没权限就别发那个请求（否则只收到一个 403，页面还白转圈）；
 *   2. 结构复杂的操作位 —— 比如表格行里的 <a>、下拉菜单项、上传组件，
 *      得自己把 :disabled 与 tooltip 拼上去，用 can() / tip() 配对；
 *   3. 弹窗底部的「确定」按钮 —— 它由 a-modal 自己渲染，v-perm 摸不到，
 *      只能靠 okProps() 把 disabled 递进 ok-button-props。
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
  /**
   * 弹窗「确定」按钮的权限化 props。
   *
   * extra 用来叠页面自己的设定（如删除弹窗的 danger）；两边任一要求禁用就禁用。
   * 光禁按钮不够：回车也能提交，所以处理函数里还要配一道 can() / need() 兜底。
   */
  const okProps = (code: string | string[], extra: Record<string, unknown> = {}) => ({
    ...extra,
    disabled: !can(code) || extra.disabled === true,
  });

  return { auth, can, canAny, tip, need, okProps };
}
