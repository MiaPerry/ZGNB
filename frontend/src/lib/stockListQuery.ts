import type { StockListParams } from '../api/types';
import { pctColor } from './formatters.ts';

function choice<T extends string>(value: string | null, options: readonly T[], fallback: T): T {
  return options.includes(value as T) ? value as T : fallback;
}

/** URL 是列表状态的唯一来源，浏览器后退和刷新均可恢复。 */
export function parseStockListQuery(search: URLSearchParams): StockListParams {
  const page = Number(search.get('page'));
  const pageSize = Number(search.get('page_size'));
  return {
    market: choice(search.get('market'), ['美股', '港股', '美股指数'], '美股'),
    q: (search.get('q') ?? '').trim().slice(0, 100),
    industry: search.has('industry') ? search.get('industry')!.slice(0, 100) : undefined,
    data_status: choice(search.get('data_status'), ['all', 'available', 'missing'], 'all'),
    sort_by: choice(search.get('sort_by'), ['ts_code', 'pct_chg', 'vol'], 'ts_code'),
    order: choice(search.get('order'), ['asc', 'desc'], 'asc'),
    page: Number.isInteger(page) && page > 0 && page <= 1000000 ? page : 1,
    page_size: [25, 50, 100].includes(pageSize) ? pageSize : 25,
  };
}

export function updateStockListQuery(
  current: URLSearchParams, changes: Partial<StockListParams>,
): URLSearchParams {
  const next = new URLSearchParams(current);
  if ('market' in changes) next.delete('industry');
  for (const [key, value] of Object.entries(changes)) {
    if (value === undefined || (key === 'q' && value === '')) next.delete(key);
    else next.set(key, String(value));
  }
  if (!('page' in changes)) next.delete('page');
  return next;
}

/** 统一沿用红涨绿跌，与详情共用涨跌色；缺失值仍单独显示。 */
export function stockChangeColor(value: number | null): string {
  if (value === null) return 'text-text-muted';
  return pctColor(value);
}
