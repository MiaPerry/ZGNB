import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchTrades, addTrade, deleteTrade, fetchTradeStats } from '../api/trade';
import Card from '../components/ui/Card';
import Button from '../components/ui/Button';
import Badge from '../components/ui/Badge';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import ApiErrorState from '../components/ui/ApiErrorState';
import { apiErrorMessage } from '../api/client';
import { formatAssetPrice, formatTradeTotal } from '../lib/formatters';
import { useAppStore } from '../stores/appStore';
import type { WorkspaceMarket } from '../stores/appStore';
import { marketKey, submittedDraftMatches } from '../lib/syncLinkage';
import { useMarketDraft } from '../hooks/useMarketDraft';

export default function Trades() {
  const queryClient = useQueryClient();
  const market = useAppStore((s) => s.currentMarket);
  const initialized = useAppStore((s) => s.marketInitialized);
  const [inputText, setInputText] = useState('');
  const [page, setPage] = useState(1);

  const { data: tradeList, isLoading, isError, error, refetch } = useQuery({
    queryKey: marketKey(market, 'trades', page),
    queryFn: ({ signal }) => fetchTrades(market, page, 20, signal),
    enabled: initialized,
  });

  const { data: stats, error: statsError } = useQuery({
    queryKey: marketKey(market, 'trade-stats'),
    queryFn: ({ signal }) => fetchTradeStats(market, signal),
    enabled: initialized,
  });

  const addMutation = useMutation({
    mutationFn: (request: { market: WorkspaceMarket; text: string }) => addTrade(request.market, request.text),
    onSuccess: (_data, request) => {
      void queryClient.invalidateQueries({ queryKey: marketKey(request.market, 'trades') });
      void queryClient.invalidateQueries({ queryKey: marketKey(request.market, 'trade-stats') });
      setInputText((current) => submittedDraftMatches(
        useAppStore.getState().currentMarket, request.market, [current], [request.text],
      ) ? '' : current);
    },
  });

  useMarketDraft(Boolean(inputText.trim()) && !(addMutation.isPending && addMutation.variables?.market === market && addMutation.variables.text === inputText));

  const deleteMutation = useMutation({
    mutationFn: (request: { market: WorkspaceMarket; id: number }) => deleteTrade(request.market, request.id),
    onSuccess: (_data, request) => {
      void queryClient.invalidateQueries({ queryKey: marketKey(request.market, 'trades') });
      void queryClient.invalidateQueries({ queryKey: marketKey(request.market, 'trade-stats') });
    },
  });

  if (isLoading) {
    return <div className="flex items-center justify-center h-96"><LoadingSpinner size="lg" /></div>;
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold text-text-primary">交易记录 · {market}</h1>
      <p className="text-xs text-text-muted">保留个人记录的原始成交价格；逐笔按标的原币展示，不进行汇率换算。{market === 'US' && '工作空间包含 USD / HKD；兼容统计为原币数值，混合币种合计不代表统一币种收益。'}</p>

      {/* Input */}
      <Card title="记录交易">
        <div className="flex gap-3">
          <input
            type="text"
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            placeholder={market === 'CN' ? '如：2026-04-25 买入 600519.SH 100股，单价1800元' : '如：2026-04-25 买入 AAPL.US 10股，单价180美元'}
            className="flex-1 rounded border border-border bg-bg-primary px-3 py-1.5 text-sm text-text-primary placeholder-text-muted outline-none focus:border-accent-gold"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && initialized && !addMutation.isPending && inputText.trim()) {
                addMutation.mutate({ market, text: inputText });
              }
            }}
          />
          <Button
            onClick={() => addMutation.mutate({ market, text: inputText })}
            disabled={!initialized || !inputText.trim() || addMutation.isPending}
          >
            保存
          </Button>
        </div>
        {addMutation.variables?.market === market && addMutation.isSuccess && (
          <div className="mt-2 text-xs text-accent-green">保存成功</div>
        )}
        {addMutation.variables?.market === market && addMutation.isError && (
          <div role="alert" className="mt-2 text-xs text-accent-red">保存失败：{apiErrorMessage(addMutation.error)}</div>
        )}
      </Card>

      {deleteMutation.variables?.market === market && deleteMutation.isError && <p role="alert" className="text-sm text-accent-red">删除失败：{apiErrorMessage(deleteMutation.error)}</p>}
      {statsError && <p role="alert" className="text-sm text-accent-red">统计加载失败：{apiErrorMessage(statsError)}</p>}
      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-4 gap-3">
          <Card>
            <div className="text-xs text-text-muted">买入总额</div>
            <div className="text-lg font-bold text-text-primary mt-1">
              {formatTradeTotal((stats.pnl as Record<string, number>)?.buy_total, market)}
            </div>
          </Card>
          <Card>
            <div className="text-xs text-text-muted">卖出总额</div>
            <div className="text-lg font-bold text-text-primary mt-1">
              {formatTradeTotal((stats.pnl as Record<string, number>)?.sell_total, market)}
            </div>
          </Card>
          <Card>
            <div className="text-xs text-text-muted">当前持仓</div>
            <div className="text-lg font-bold text-accent-gold mt-1">
              {(stats.pnl as Record<string, number>)?.current_qty || 0} 股
            </div>
          </Card>
          <Card>
            <div className="text-xs text-text-muted">已实现盈亏</div>
            {(() => {
              const pnl = (stats.pnl as Record<string, number>)?.realized_pnl || 0;
              const hasTrades = ((stats.pnl as Record<string, number>)?.buy_total || 0) > 0
                || ((stats.pnl as Record<string, number>)?.sell_total || 0) > 0;
              const tone = !hasTrades
                ? 'text-text-muted'
                : pnl > 0
                  ? 'text-up'
                  : pnl < 0
                    ? 'text-down'
                    : 'text-text-primary';
              return (
                <div className={`text-lg font-bold mt-1 ${tone}`}>
                  {formatTradeTotal(pnl, market)}
                </div>
              );
            })()}
          </Card>
        </div>
      )}

      {/* Trade List */}
      <Card title={`交易记录 (${tradeList?.total || 0})`}>
        {isError ? <ApiErrorState message={apiErrorMessage(error)} onRetry={() => void refetch()} /> : !tradeList || tradeList.records.length === 0 ? (
          <div className="text-center py-8 text-text-muted">暂无交易记录</div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-text-muted">
                    <th className="text-left py-2 px-2">日期</th>
                    <th className="text-left py-2 px-2">代码</th>
                    <th className="text-left py-2 px-2">方向</th>
                    <th className="text-right py-2 px-2">价格</th>
                    <th className="text-right py-2 px-2">数量</th>
                    <th className="text-right py-2 px-2">金额</th>
                    <th className="text-left py-2 px-2">原因</th>
                    <th className="text-right py-2 px-2">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {tradeList.records.map((r) => (
                    <tr key={r.id} className="border-b border-border/50 hover:bg-bg-hover/50">
                      <td className="py-2 px-2 text-text-secondary">{r.trade_date}</td>
                      <td className="py-2 px-2 font-mono text-accent-gold">{r.ts_code}</td>
                      <td className="py-2 px-2">
                        <Badge variant={r.action === 'BUY' ? 'success' : 'danger'}>
                          {r.action === 'BUY' ? '买入' : '卖出'}
                        </Badge>
                      </td>
                      <td className="py-2 px-2 text-right font-mono">{formatAssetPrice(r.price, market, r.ts_code)}</td>
                      <td className="py-2 px-2 text-right font-mono">{r.quantity}</td>
                      <td className="py-2 px-2 text-right font-mono">{formatAssetPrice(r.amount, market, r.ts_code)}</td>
                      <td className="py-2 px-2 text-text-muted text-xs max-w-48 truncate">{r.reason}</td>
                      <td className="py-2 px-2 text-right">
                        <button
                          onClick={() => deleteMutation.mutate({ market, id: r.id })}
                          className="text-xs text-accent-red hover:underline"
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {/* Pagination */}
            <div className="flex items-center justify-between mt-3 pt-3 border-t border-border">
              <span className="text-xs text-text-muted">
                第 {tradeList.page} 页 / 共 {tradeList.total} 条
              </span>
              <div className="flex gap-2">
                <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                  上一页
                </Button>
                <Button size="sm" variant="ghost" disabled={page * tradeList.page_size >= tradeList.total} onClick={() => setPage(page + 1)}>
                  下一页
                </Button>
              </div>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
