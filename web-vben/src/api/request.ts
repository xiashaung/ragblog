/**
 * 请求封装 —— 兼容后端统一 envelope 的 axios 实例。
 *
 * 后端所有 2xx 返回 { code: 0, data, message }，错误返回 { code: <http>, data: null, message: <中文> }。
 * 这里把 envelope.data 平移到 res.data（页面直接读 .data），并解析错误中文 message。
 *
 * 鉴权走 Bearer token（管理后台 admin 体系）与 X-API-Key（OpenAI 兼容接口）两套，
 * 由 @/api/http 按接口前缀自动注入。
 */
import axios, { type AxiosInstance, type AxiosRequestConfig, type AxiosResponse } from 'axios';

/**
 * admin 体系接口已收进 /api/v1/admin（见后端 app/routers/admin.py），/admin 路径
 * 纯留给控制台 SPA 页面。两个实例的 baseURL 都是 /api/v1，页面调用时只需再拼
 * 各自子路径（如 /admin/login → /api/v1/admin/login）。
 */
/**
 * 这些接口的 401 属于「凭据本身不对」，不是控制台会话失效，不该清 token + 弹窗：
 *   - /ai/keys/me：X-API-Key 自校验，401 = Key 无效；
 *   - /admin/login、/admin/onboard：密码错 / 引导密钥错，页面自己显示内联错误。
 */
const SILENT_401_PATHS = ['/ai/keys/me', '/admin/login', '/admin/onboard'];

function isSilent401(url?: string): boolean {
  return !!url && SILENT_401_PATHS.some((path) => url.includes(path));
}

export function applyBaseInterceptors(instance: AxiosInstance, base: string): void {
  instance.interceptors.request.use((config) => {
    const key = localStorage.getItem('api_key') || '';
    if (key) { config.headers = config.headers ?? {}; }
    if (key) { (config.headers as Record<string, string>)['X-API-Key'] = key; }
    const token = localStorage.getItem('admin_token') || '';
    if (token) { (config.headers as Record<string, string>)['Authorization'] = `Bearer ${token}`; }
    return config;
  });
  // 成功：把 envelope.data 平移到 response.data
  instance.interceptors.response.use((response: AxiosResponse) => {
    const data = response.data;
    if (data && typeof data === 'object' && 'code' in data) {
      response.data = (data as { data?: any }).data ?? null;
      (response as any).envelope = data;
    }
    return response;
  }, (error) => {
    const response = error.response;
    if (response && response.data && typeof response.data === 'object' && 'message' in response.data) {
      const body = response.data;
      response.data = (body as { data?: any }).data ?? null;
      (error as any).detail = body.message || (error as any).detail;
      (error as any).message = body.message;
    }
    // 401 统一失效：清 token 并跳转登录
    // 例外见 SILENT_401_PATHS（否则密码错会被误报成「登录已失效」）
    if (response?.status === 401 && !isSilent401((error.config as any)?.url)) {
      localStorage.removeItem('admin_token');
      localStorage.removeItem('user_info');
      localStorage.removeItem('user_roles');
      localStorage.removeItem('user_permissions');
      const modal = (globalThis as any).modal;
      if (modal) {
        modal.error({
          title: '登录已失效',
          content: (error as any).detail || '请重新登录',
          onOk: () => { location.href = `${import.meta.env.BASE_URL}auth/login`; },
        });
      }
    }
    return Promise.reject(error);
  });
}

// admin 实例：baseURL /api/v1 + 路径 `/admin/...` → 最终 /api/v1/admin/...
export const httpAdmin = axios.create({ baseURL: '/api/v1', timeout: 60_000, withCredentials: true });
applyBaseInterceptors(httpAdmin, '/api/v1');

// 其余接口实例：前缀 /api/v1
export const http = axios.create({ baseURL: '/api/v1', timeout: 60_000, withCredentials: true });
applyBaseInterceptors(http, '/api/v1');

export default http;

/**
 * 带 loading 的封装调用；失败统一抛中文 message。
 * 页面通过 `api.xxx()` 调用，内部自动套 loading。
 */
export function request<T = any>(config: AxiosRequestConfig): Promise<T> {
  return http.request<T>(config).then((res) => res.data as T);
}

/**
 * 同 ``request``，但走 admin 实例（/admin 前缀，Bearer 会话）。
 * RBAC 相关的账号/角色/权限接口都用它，拿到即 payload。
 */
export function adminRequest<T = any>(config: AxiosRequestConfig): Promise<T> {
  return httpAdmin.request<T>(config).then((res) => res.data as T);
}
