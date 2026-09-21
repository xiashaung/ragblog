/**
 * v-perm / v-perm-any —— 按钮级权限指令（在 main.ts 里全局注册）。
 *
 * 用法：
 *   <a-button v-perm="'knowledge:manage'">录入文档</a-button>
 *   <a-button v-perm="['account:manage', 'role:read']">两者都要有</a-button>
 *   <a-button v-perm-any="['system:manage', 'system:read']">有任一即可</a-button>
 *
 * 无权时不摘掉节点，而是「禁用 + 悬停说明缺哪条」：凭空少一个按钮，用户只会以为功能坏了，
 * 摆在那里禁用掉反而更容易讲清是权限问题。真正的拦截始终在后端（app/deps.require_permission），
 * 这一层只管渲染，绕不绕得过都不影响安全。
 *
 * 为什么不用 v-if：见上面那段。为什么不用 antd 的 Tooltip 组件：包一层 Tooltip 要改
 * 每个按钮的模板结构，而指令对 <a-button> / <Button> / 原生 <button> 一律可用。
 */
import type { App, Directive, DirectiveBinding } from 'vue';
import { ref, watchEffect, type Ref } from 'vue';
import { useAuthStore } from '@/store/modules/auth';
import { permTip } from '@/config/permissions';

/** 挂在元素上的一份内部状态（卸载时用它停掉副作用、还原属性） */
interface PermState {
  /** 当前声明的权限码；换成响应式的是为了让 watchEffect 能感知到模板里的值变了 */
  codes: Ref<string[]>;
  anyMode: boolean;
  stop: () => void;
  allowed: boolean;
  /** 记录「disabled / title 是不是我们加上去的」，还原时才不会踩掉页面自己的声明 */
  disabledByUs: boolean;
  prevTitle: string | null;
}

const STATE = Symbol('perm-directive');

/** 浏览器只对表单类元素认 ``disabled`` 属性，其余靠拦截点击。 */
const NATIVELY_DISABLEABLE = new Set(['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'OPTION']);

function normalize(value: unknown): string[] {
  if (Array.isArray(value)) return value.filter((item): item is string => !!item);
  return value ? [String(value)] : [];
}

function apply(el: HTMLElement, state: PermState) {
  const codes = state.codes.value;
  const auth = useAuthStore();
  // 没写权限码（空数组）等于不设限，直接放行，避免「忘了填」把按钮锁死
  state.allowed = !codes.length || (state.anyMode ? auth.hasAnyPerm(codes) : auth.hasPerm(codes));

  if (state.allowed) {
    el.classList.remove('perm-disabled');
    if (state.disabledByUs) {
      el.removeAttribute('disabled');
      state.disabledByUs = false;
    }
    if (state.prevTitle !== null) {
      el.setAttribute('title', state.prevTitle);
      state.prevTitle = null;
    }
    return;
  }

  el.classList.add('perm-disabled');
  if (NATIVELY_DISABLEABLE.has(el.tagName) && !el.hasAttribute('disabled')) {
    el.setAttribute('disabled', '');
    el.setAttribute('aria-disabled', 'true');
    state.disabledByUs = true;
  }
  // 原生 title 就够用了：它不要求把按钮包进 Tooltip，也就不改动布局
  if (state.prevTitle === null) state.prevTitle = el.getAttribute('title');
  el.setAttribute('title', permTip(codes, state.anyMode));
}

/** 点击兜底：非表单元素没有 disabled，所以在捕获阶段把事件吞掉。 */
function guard(event: Event, state: PermState) {
  if (!state.allowed) {
    event.preventDefault();
    event.stopPropagation();
  }
}

function setup(el: HTMLElement, binding: DirectiveBinding, anyMode: boolean) {
  const state: PermState = {
    codes: ref(normalize(binding.value)),
    anyMode,
    stop: () => {},
    allowed: true,
    disabledByUs: false,
    prevTitle: null,
  };
  // flush: 'post' —— 等组件把 DOM 打完再改属性，否则我们写的 disabled 会被 Vue 覆盖掉
  // （hasPerm 内部读的就是响应式权限集，所以刷新 / 登录后权限变了也会重算一次）
  state.stop = watchEffect(() => apply(el, state), { flush: 'post' });
  const onClick = (event: Event) => guard(event, state);
  el.addEventListener('click', onClick, true);
  // 卸载时要停掉副作用并解绑，否则切换页面会留下对已销毁元素的写入
  (el as any)[STATE] = { state, onClick };
}

function restore(el: HTMLElement) {
  const holder = (el as any)[STATE] as { state: PermState; onClick: (e: Event) => void } | undefined;
  if (!holder) return;
  holder.state.stop();
  el.removeEventListener('click', holder.onClick, true);
  el.classList.remove('perm-disabled');
  if (holder.state.disabledByUs) el.removeAttribute('disabled');
  if (holder.state.prevTitle !== null) el.setAttribute('title', holder.state.prevTitle);
  delete (el as any)[STATE];
}

function makeDirective(anyMode: boolean): Directive<HTMLElement> {
  return {
    mounted: (el, binding) => setup(el, binding, anyMode),
    updated: (el, binding) => {
      const holder = (el as any)[STATE] as { state: PermState } | undefined;
      if (!holder) return;
      const next = normalize(binding.value);
      if (next.join(',') !== holder.state.codes.value.join(',')) {
        holder.state.codes.value = next; // 触发上面的 watchEffect 重算
      }
    },
    unmounted: restore,
  };
}

/** 要求列出的权限码全部命中。 */
export const vPerm = makeDirective(false);
/** 列出的权限码命中任一即可。 */
export const vPermAny = makeDirective(true);

export default {
  install(app: App) {
    app.directive('perm', vPerm);
    app.directive('perm-any', vPermAny);
  },
};
