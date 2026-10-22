import axios from 'axios';
import type { WorkspaceMarket } from '../stores/appStore.ts';

/** 调用者必须传入启动时捕获的市场；重试/轮询不读取可变全局状态。 */
export function marketRequest(market: WorkspaceMarket, signal?: AbortSignal) {
  if (market !== 'US' && market !== 'CN') throw new Error('请求缺少有效工作空间市场');
  return { headers: { 'X-Market': market }, signal };
}

export function apiErrorMessage(error: unknown): string {
  const detail = axios.isAxiosError(error) ? error.response?.data?.detail : null;
  return typeof detail === 'string' ? detail : error instanceof Error ? error.message : '请求失败，请重试';
}

/** 已确认缺库不自动重试，避免后台标签页的暂停重试遮住明确错误。 */
export function shouldRetryDataStatus(failureCount: number, error: unknown): boolean {
  if (axios.isAxiosError(error) && error.response?.status === 503
    && error.response?.data?.error === 'market_unavailable') return false;
  return failureCount < 1;
}

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 120000,
  headers: { 'Content-Type': 'application/json' },
});

// 响应拦截器
api.interceptors.response.use(
  (res) => res,
  (err) => {
    const msg = err.response?.data?.detail || err.message || '请求失败';
    console.error('[API Error]', msg);
    return Promise.reject(err);
  }
);

export default api;
