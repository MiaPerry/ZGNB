import type { StockListParams } from '../api/types';
import { pctColor } from './formatters.ts';
import { codeBelongsToMarket } from '../stores/appStore.ts';
import type { WorkspaceMarket } from '../stores/appStore.ts';

const US_CATEGORIES = ['美股', '港股', '美股指数'] as const;

function validCategory(value: string, market: WorkspaceMarket, categories?: readonly string[]): boolean {
  if (market === 'US') return US_CATEGORIES.includes(value as typeof US_CATEGORIES[number]);
  if (value === '全部A股') return true;
  if (US_CATEGORIES.includes(value as typeof US_CATEGORIES[number])) return false;
  return categories ? categories.includes(value) : value.length > 0 && value.length <= 100;
}

/** 所有列表入口统一清理工作空间相关参数；字典未返回时不猜测 CN 板块。 */
export function cleanStockListQuery(
  search: URLSearchParams, market: WorkspaceMarket,
  categories?: readonly string[], industries?: readonly string[], switching = false,
): URLSearchParams {
  const next = new URLSearchParams(search);
  const category = next.get('market');
  if (switching || (category !== null && !validCategory(category, market, categories))) {
    for (const key of ['market', 'industry', 'q', 'page']) next.delete(key);
  }
  if (industries && next.has('industry') && !industries.includes(next.get('industry')!)) {
    next.delete('industry');
    next.delete('page');
  }
  for (const [key, allowed] of [
    ['page_size', ['25', '50', '100']], ['sort_by', ['ts_code', 'pct_chg', 'vol']],
    ['order', ['asc', 'desc']], ['data_status', ['all', 'available', 'missing']],
  ] as const) {
    if (next.has(key) && !allowed.some((value) => value === next.get(key))) next.delete(key);
  }
  return next;
}

export function stockListReturnTarget(state: unknown, market: WorkspaceMarket): string {
  const value = state as { stockListSearch?: unknown; stockListMarket?: unknown } | null;
  if (value?.stockListMarket !== market || typeof value.stockListSearch !== 'string') return '/stocks';
  const search = cleanStockListQuery(new URLSearchParams(value.stockListSearch), market).toString();
  return `/stocks${search ? `?${search}` : ''}`;
}

/** 详情校验必须在挂载业务 Outlet（包括付费点评）之前完成。 */
export function stockDetailDecision(pathname: string, market: WorkspaceMarket, initialized: boolean): 'wait' | 'allow' | 'reject' {
  if (!initialized) return 'wait';
  if (!pathname.startsWith('/stock/')) return 'allow';
  try {
    return codeBelongsToMarket(decodeURIComponent(pathname.slice(7)), market) ? 'allow' : 'reject';
  } catch { return 'reject'; }
}

function choice<T extends string>(value: string | null, options: readonly T[], fallback: T): T {
  return options.includes(value as T) ? value as T : fallback;
}

/** URL 是列表状态的唯一来源，浏览器后退和刷新均可恢复。 */
export function parseStockListQuery(search: URLSearchParams, market: WorkspaceMarket = 'US', categories?: readonly string[]): StockListParams {
  search = cleanStockListQuery(search, market, categories);
  const page = Number(search.get('page'));
  const pageSize = Number(search.get('page_size'));
  return {
    market: search.get('market') ?? (market === 'CN' ? '全部A股' : '美股'),
    q: (search.get('q') ?? '').trim().slice(0, 100),
    industry: search.has('industry') ? search.get('industry')!.slice(0, 100) : undefined,
    data_status: choice(search.get('data_status'), ['all', 'available', 'missing'], 'all'),
    sort_by: choice(search.get('sort_by'), ['ts_code', 'pct_chg', 'vol'], 'ts_code'),
    order: choice(search.get('order'), ['asc', 'desc'], 'asc'),
    page: Number.isInteger(page) && page > 0 && page <= 1000000 ? page : 1,
    page_size: [25, 50, 100].includes(pageSize) ? pageSize : 25,
  };
}

/** 分类元数据先于过滤请求；不以其他分类或过滤后的结果猜测行业。 */
export function stockListRequestGate(
  search: URLSearchParams, market: WorkspaceMarket, categories?: readonly string[],
  dictionary?: { market: string; industries: readonly string[] },
) {
  const categoryCleaned = cleanStockListQuery(search, market, categories);
  const category = parseStockListQuery(categoryCleaned, market, categories).market;
  const dictionaryParams = { ...parseStockListQuery(new URLSearchParams(), market), market: category };
  const verified = categories !== undefined && dictionary?.market === category;
  const cleaned = cleanStockListQuery(categoryCleaned, market, categories, verified ? dictionary.industries : undefined);
  const cleanSearch = cleaned.toString();
  return {
    dictionaryParams, cleanSearch, params: parseStockListQuery(cleaned, market, categories),
    enabled: verified && cleanSearch === search.toString(),
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
