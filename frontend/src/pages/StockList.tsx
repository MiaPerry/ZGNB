import { Link, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { fetchStockList } from '../api/stock';
import { addToWatchlist } from '../api/watchlist';
import type { StockListParams, StockMarket, StockSort, StockDataStatus } from '../api/types';
import Card from '../components/ui/Card';
import Button from '../components/ui/Button';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import ApiErrorState from '../components/ui/ApiErrorState';
import { formatNumber, formatPct, formatVolume } from '../lib/formatters';
import { parseStockListQuery, updateStockListQuery, stockChangeColor } from '../lib/stockListQuery';

const inputClass = 'rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm text-text-primary outline-none focus:border-accent-gold';
const disabledClass = 'disabled:opacity-40 disabled:cursor-not-allowed';

function dateLabel(date: string | null | undefined): string {
  return date ? `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}` : '--';
}

function errorMessage(error: unknown): string {
  const detail = axios.isAxiosError(error) ? error.response?.data?.detail : null;
  return typeof detail === 'string' ? detail : error instanceof Error ? error.message : '请求失败，请重试';
}

export default function StockList() {
  const [search, setSearch] = useSearchParams();
  const params = parseStockListQuery(search);
  const queryClient = useQueryClient();
  // 复用行情查询前缀，同步完成后现有刷新逻辑会自动更新股票库。
  const { data, isLoading, isFetching, isError, error, refetch } = useQuery({
    queryKey: ['stock', 'list', params],
    queryFn: ({ signal }) => fetchStockList(params, signal),
  });
  const addMutation = useMutation({
    mutationFn: (code: string) => addToWatchlist(code),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['watchlist'] }),
        queryClient.invalidateQueries({ queryKey: ['stock', 'list'] }),
      ]);
    },
  });

  const update = (changes: Partial<StockListParams>) => setSearch(updateStockListQuery(search, changes));
  const reset = () => setSearch(new URLSearchParams({ market: params.market }));
  const totalPages = Math.max(1, Math.ceil((data?.total ?? 0) / params.page_size));
  const priceUnit = params.market === '港股' ? 'HKD' : params.market === '美股指数' ? '点' : 'USD';
  const markets = data?.markets ?? [];
  const industries = data?.industries ?? [];
  const sort = (key: StockSort) => update({
    sort_by: key,
    order: params.sort_by === key ? (params.order === 'asc' ? 'desc' : 'asc') : key === 'ts_code' ? 'asc' : 'desc',
  });
  const sortHeading = (key: StockSort, label: string) => (
    <th className={`px-3 py-3 ${key === 'ts_code' ? 'text-left' : 'text-right'}`}
      aria-sort={params.sort_by === key ? (params.order === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button onClick={() => sort(key)} className="hover:text-accent-gold transition-colors" title={`按${label}排序`}>
        {label} <span aria-hidden="true">{params.sort_by === key ? (params.order === 'asc' ? '↑' : '↓') : '↕'}</span>
      </button>
    </th>
  );

  return (
    <div className="min-w-0 space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-text-primary">股票库</h1>
          <p className="mt-2 flex items-start gap-2 text-xs leading-5 text-accent-gold/90">
            <svg aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
              <circle cx="12" cy="12" r="9" /><path d="M12 7v6m0 3v1" />
            </svg>
            仅展示系统已收录标的，不代表全市场；价格为最近日线收盘价，非实时行情，请以各行交易日期为准。
          </p>
        </div>
        <Button variant="secondary" onClick={() => void refetch()} disabled={isFetching} className={disabledClass}>
          {isFetching ? '刷新中…' : '刷新列表'}
        </Button>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Card className="hover:translate-y-0">
          <div className="text-xs text-text-muted">当前分类已收录 · {params.market}</div>
          <div className="mt-2 text-2xl font-bold tabular-nums text-accent-gold">{data?.market_total ?? '--'} <span className="text-xs font-normal text-text-muted">个标的</span></div>
        </Card>
        <Card className="hover:translate-y-0">
          <div className="text-xs text-text-muted">符合当前条件</div>
          <div className="mt-2 text-2xl font-bold tabular-nums">{data?.total ?? '--'} <span className="text-xs font-normal text-text-muted">个标的</span></div>
        </Card>
        <Card className="hover:translate-y-0">
          <div className="text-xs text-text-muted">当前分类库内最新交易日</div>
          <div className="mt-2 text-xl font-semibold font-mono">{dateLabel(data?.latest_trade_date)}</div>
        </Card>
      </div>

      <Card className="hover:translate-y-0">
        <form key={params.q} className="flex flex-wrap gap-2" onSubmit={(event) => {
          event.preventDefault();
          update({ q: String(new FormData(event.currentTarget).get('q') ?? '').trim() });
        }}>
          <input name="q" aria-label="搜索代码或名称" defaultValue={params.q} maxLength={100}
            placeholder="搜索代码或名称，如 AAPL / 苹果" className={`${inputClass} min-w-0 flex-1 basis-60`} />
          <Button type="submit">搜索</Button>
          <Button type="button" variant="ghost" onClick={(event) => { event.currentTarget.form?.reset(); reset(); }}>重置</Button>
        </form>
        <div className="mt-4 flex flex-wrap items-end gap-4">
          <label className="flex flex-col gap-1.5 text-xs text-text-muted">
            市场分类
            <select aria-label="市场分类" value={params.market} onChange={(e) => update({ market: e.target.value as StockMarket })} className={inputClass}>
              {!markets.some((m) => m.market === params.market) && <option value={params.market}>{params.market}</option>}
              {markets.map((m) => <option key={m.market} value={m.market}>{m.market}（{m.count}）</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1.5 text-xs text-text-muted">
            行业
            <select aria-label="行业" value={params.industry === undefined ? 'all' : `industry:${params.industry}`}
              onChange={(e) => update({ industry: e.target.value === 'all' ? undefined : e.target.value.slice(9) })}
              className={`${inputClass} max-w-64`}>
              <option value="all">全部行业</option>
              {params.industry !== undefined && !industries.includes(params.industry) && (
                <option value={`industry:${params.industry}`}>{params.industry || '未分类'}</option>
              )}
              {industries.map((industry) => <option key={industry} value={`industry:${industry}`}>{industry || '未分类'}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1.5 text-xs text-text-muted">
            行情数据
            <select aria-label="行情数据" value={params.data_status}
              onChange={(e) => update({ data_status: e.target.value as StockDataStatus })} className={inputClass}>
              <option value="all">全部状态</option><option value="available">有行情</option><option value="missing">暂无可用行情</option>
            </select>
          </label>
          <p className="pb-2 text-xs text-text-muted">点击表头排序 · 红涨绿跌 · 不同交易日期的涨跌幅仅供参考</p>
        </div>
      </Card>

      {addMutation.isError && <p role="alert" className="text-sm text-accent-red">加入自选失败：{errorMessage(addMutation.error)}</p>}
      {addMutation.isSuccess && <p role="status" className="text-sm text-accent-green">{addMutation.variables} 已加入自选</p>}

      {isError ? <ApiErrorState message={errorMessage(error)} onRetry={() => void refetch()} /> : (
        <div className="overflow-hidden rounded-xl border border-border/40 bg-bg-card" aria-busy={isFetching}>
          {isLoading ? (
            <div className="flex justify-center py-20"><LoadingSpinner text="加载股票库…" /></div>
          ) : !data?.items.length ? (
            <div className="space-y-3 py-16 text-center text-sm text-text-muted">
              <p>{data?.total ? '该页暂无数据，请返回第一页。' : data?.market_total ? '没有符合条件的股票，试试其他关键词或筛选条件。' : '当前分类暂无收录标的。'}</p>
              <Button variant="secondary" onClick={() => data?.total ? update({ page: 1 }) : reset()}>
                {data?.total ? '返回第一页' : '清除筛选'}
              </Button>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[940px] text-sm">
                <caption className="sr-only">系统收录股票及最近日线行情</caption>
                <thead className="border-b border-border bg-bg-secondary text-xs text-text-muted">
                  <tr>
                    {sortHeading('ts_code', '代码')}
                    <th className="px-3 py-3 text-left">名称</th>
                    <th className="px-3 py-3 text-left">行业</th>
                    <th className="px-3 py-3 text-right">最近收盘（{priceUnit}）</th>
                    {sortHeading('pct_chg', '涨跌幅')}
                    {sortHeading('vol', params.market === '美股指数' ? '成交量（源数据）' : '成交量（股）')}
                    <th className="px-3 py-3 text-left">交易日期</th>
                    <th className="px-3 py-3 text-left">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((stock) => (
                    <tr key={stock.ts_code} className="border-b border-border/30 last:border-0 hover:bg-bg-hover/40">
                      <td className="px-3 py-3 font-mono font-semibold text-accent-gold">{stock.ts_code}</td>
                      <td className="max-w-48 truncate px-3 py-3" title={stock.name}>{stock.name}</td>
                      <td className="max-w-36 truncate px-3 py-3 text-xs text-text-secondary" title={stock.industry}>{stock.industry || '未分类'}</td>
                      <td className="px-3 py-3 text-right font-mono">{stock.close === null ? '--' : formatNumber(stock.close)}</td>
                      <td className={`px-3 py-3 text-right font-mono ${stockChangeColor(stock.pct_chg)}`}>{stock.pct_chg === null ? '--' : formatPct(stock.pct_chg)}</td>
                      <td className="px-3 py-3 text-right font-mono text-text-secondary">{stock.vol === null ? '--' : formatVolume(stock.vol)}</td>
                      <td className="whitespace-nowrap px-3 py-3 text-xs text-text-secondary">
                        {dateLabel(stock.trade_date)}
                        {stock.data_status === 'missing' && <div className="mt-1 text-accent-gold">暂无可用行情</div>}
                      </td>
                      <td className="whitespace-nowrap px-3 py-3">
                        <div className="flex items-center gap-3">
                          {stock.data_status === 'available' ? (
                            <Link to={`/stock/${stock.ts_code}`} state={{ stockListSearch: search.toString() }}
                              className="text-xs text-accent-blue hover:underline" aria-label={`分析 ${stock.ts_code}`}>分析</Link>
                          ) : <span className="text-xs text-text-muted" title="缺少可用行情，暂不能分析">分析不可用</span>}
                          <button disabled={stock.is_watchlisted || addMutation.isPending}
                            onClick={() => addMutation.mutate(stock.ts_code)}
                            aria-label={`${stock.is_watchlisted ? '已自选' : '加入自选'} ${stock.ts_code}`}
                            className={`text-xs ${stock.is_watchlisted ? 'text-text-muted' : 'text-accent-gold hover:underline'} ${disabledClass}`}>
                            {stock.is_watchlisted ? '★ 已自选' : addMutation.isPending && addMutation.variables === stock.ts_code ? '添加中…' : '☆ 加自选'}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border/40 px-4 py-3 text-xs text-text-muted">
            <span aria-live="polite">共 {data?.total ?? 0} 个 · 第 {params.page} / {totalPages} 页</span>
            <div className="flex flex-wrap items-center gap-2">
              <select aria-label="每页数量" value={params.page_size} onChange={(e) => update({ page_size: Number(e.target.value) })}
                className="rounded border border-border bg-bg-primary px-2 py-1.5 text-text-secondary">
                {[25, 50, 100].map((n) => <option key={n} value={n}>{n} 条/页</option>)}
              </select>
              <Button size="sm" variant="secondary" disabled={params.page <= 1 || isFetching} onClick={() => update({ page: params.page - 1 })} className={disabledClass}>上一页</Button>
              <Button size="sm" variant="secondary" disabled={params.page >= totalPages || isFetching} onClick={() => update({ page: params.page + 1 })} className={disabledClass}>下一页</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
