/**
 * Auth Store —— 管理后台登录态 + RBAC 权限集（Vben 规范：token / user / permissions）。
 *
 * 后端约定（app/routers/admin.py、app/deps.py）：
 *   - `/admin/login` 返回 `{token, admin, roles, permissions}`，同时写 http-only cookie；
 *   - 管理接口靠 `Authorization: Bearer <token>` 鉴权，再按权限码放行；
 *   - 权限不足 → 403（中文 message），会话失效 → 401（拦截器统一跳登录）。
 * 前端存一份 roles/permissions 只为**渲染**菜单与按钮，真正的拦截在后端。
 */
import { computed } from 'vue';
import { defineStore } from 'pinia';
import { usePersistedState } from './state';
import { adminApi } from '@/api';
import type { AdminUser } from '@/typings';

/** `/admin/login` 的响应体（一次登录会话），与 typings 里的在线会话 SessionItem 不是一回事 */
export interface LoginSession {
  token: string;
  token_type: string;
  expires_at: number;
  admin?: { id: string; username: string; name: string; is_active: boolean };
  roles?: string[];
  permissions?: string[];
  username?: string;
}

const TOKEN_KEY = 'admin_token';
const USER_KEY = 'user_info';
const ROLES_KEY = 'user_roles';
const PERMS_KEY = 'user_permissions';

/** `*` 是后端给超级管理员的通配权限，前端同样识别 */
const WILDCARD = '*';

export const useAuthStore = defineStore('auth', () => {
  const token = usePersistedState<string>(TOKEN_KEY, '');
  const userInfo = usePersistedState<AdminUser>(USER_KEY, {} as AdminUser);
  const roles = usePersistedState<string[]>(ROLES_KEY, []);
  const permissions = usePersistedState<string[]>(PERMS_KEY, []);

  const isSuperadmin = computed(() => permissions.value?.includes(WILDCARD) ?? false);

  function write(key: string, value: unknown) {
    localStorage.setItem(key, JSON.stringify(value));
  }

  function applyIdentity(session: Partial<LoginSession>) {
    roles.value = session.roles ?? [];
    permissions.value = session.permissions ?? [];
    write(ROLES_KEY, roles.value);
    write(PERMS_KEY, permissions.value);
    const admin = session.admin;
    if (admin) {
      userInfo.value = { ...admin, roles: roles.value, permissions: permissions.value };
      write(USER_KEY, userInfo.value);
    }
  }

  function setSession(session: LoginSession) {
    token.value = session.token;
    // 后端登录态由 http-only cookie 承载，JS 读不到；因此同时把 token 写进
    // localStorage，保证后续请求带上 Bearer（见 api/request.ts 拦截器）。
    localStorage.setItem(TOKEN_KEY, session.token);
    applyIdentity(session);
    if (!session.admin) {
      // 兜底：响应体只有 username（无 admin 子对象）
      const name = session.username || '';
      userInfo.value = {
        id: '',
        username: name,
        name,
        is_active: true,
        roles: roles.value,
        permissions: permissions.value,
      };
      write(USER_KEY, userInfo.value);
    }
  }

  async function login(username: string, password: string) {
    const res: any = await adminApi.login({ username, password });
    const data: LoginSession = res?.data?.token ? res.data : (res?.data ?? res);
    if (!data?.token) throw new Error('登录响应缺少 token');
    setSession(data);
    return data;
  }

  /**
   * 首次引导：创建超管后直接登录。
   *
   * 后端 `/admin/onboard` 只建账号不发 token（避免引导接口变成第二个登录入口），
   * 所以这里 onboard 成功后紧一次 login。bootstrapKey 即 APP_ADMIN_API_KEY，
   * 只在引导阶段用一次，不会写进 localStorage。
   */
  async function onboard(username: string, password: string, name: string, bootstrapKey: string) {
    await adminApi.onboard({ username, password, name }, bootstrapKey);
    return login(username, password);
  }

  /**
   * 拉取 /admin/me 同步权限。
   * localStorage 里可能是一份过期权限（角色被改过），刷新页面时以服务端为准。
   */
  async function fetchMe() {
    if (!token.value) return null;
    const me = await adminApi.me();
    if (!me) return null;
    applyIdentity({
      admin: { id: me.id, username: me.username, name: me.name, is_active: true },
      roles: me.roles,
      permissions: me.permissions,
    });
    return me;
  }

  /** 单个或「全部」权限判断（数组默认要求全部命中） */
  function hasPerm(code: string | string[]): boolean {
    const owned = permissions.value || [];
    if (owned.includes(WILDCARD)) return true;
    const need = Array.isArray(code) ? code : [code];
    return need.every((item) => owned.includes(item));
  }

  /** 任一命中即可（用于「读或写任一可见」的入口） */
  function hasAnyPerm(codes: string[]): boolean {
    const owned = permissions.value || [];
    if (owned.includes(WILDCARD)) return true;
    return codes.some((item) => owned.includes(item));
  }

  function logout() {
    token.value = '';
    userInfo.value = {} as AdminUser;
    roles.value = [];
    permissions.value = [];
    [TOKEN_KEY, USER_KEY, ROLES_KEY, PERMS_KEY].forEach((key) => localStorage.removeItem(key));
  }

  function isAuthenticated() {
    return !!(token.value || userInfo.value?.username);
  }

  return {
    token,
    userInfo,
    roles,
    permissions,
    isSuperadmin,
    setSession,
    applyIdentity,
    login,
    onboard,
    fetchMe,
    hasPerm,
    hasAnyPerm,
    logout,
    isAuthenticated,
  };
});
