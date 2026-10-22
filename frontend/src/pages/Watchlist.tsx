import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { fetchWatchlist, addToWatchlist, removeFromWatchlist, scanWatchlist } from '../api/watchlist';
import Card from '../components/ui/Card';
import Button from '../components/ui/Button';
import Badge from '../components/ui/Badge';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import ApiErrorState from '../components/ui/ApiErrorState';
import { useAppStore, normalizeStockCode } from '../stores/appStore';
import { apiErrorMessage } from '../api/client';
import type { WorkspaceMarket } from '../stores/appStore';
import { marketKey, submittedDraftMatches } from '../lib/syncLinkage';
import { useMarketDraft } from '../hooks/useMarketDraft';

export default function Watchlist() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const market = useAppStore((s) => s.currentMarket);
  const initialized = useAppStore((s) => s.marketInitialized);
  const [draft, setDraft] = useState({ code: '', tags: '' });
  const { code: newCode, tags: newTags } = draft;

  const { data: watchlist, isLoading, isError, error, refetch } = useQuery({
    queryKey: marketKey(market, 'watchlist'),
    queryFn: ({ signal }) => fetchWatchlist(market, signal),
    enabled: initialized,
  });

  const [scanStarted, setScanStarted] = useState(false);

  const { data: scanResult, isFetching: scanUpdating, refetch: refetchScan } = useQuery({
    queryKey: marketKey(market, 'watchlist-scan'),
    queryFn: ({ signal }) => scanWatchlist(market, signal),
    // 运行后才启用：同步完成的联动只会重跑已执行过的扫描，不会隐式启动
    enabled: initialized && scanStarted,
    staleTime: Infinity,
  });

  const addMutation = useMutation({
    mutationFn: (request: { market: WorkspaceMarket; code: string; tags: string }) => addToWatchlist(request.market, normalizeStockCode(request.code, request.market), request.tags),
    onSuccess: (_data, request) => {
      void queryClient.invalidateQueries({ queryKey: marketKey(request.market, 'watchlist') });
      void queryClient.invalidateQueries({ queryKey: marketKey(request.market, 'stock', 'list') });
      setDraft((current) => submittedDraftMatches(
        useAppStore.getState().currentMarket, request.market,
        [current.code, current.tags], [request.code, request.tags],
      ) ? { code: '', tags: '' } : current);
    },
  });

  useMarketDraft(Boolean(newCode.trim() || newTags.trim()) && !(addMutation.isPending && addMutation.variables?.market === market && addMutation.variables.code === newCode && addMutation.variables.tags === newTags));

  const removeMutation = useMutation({
    mutationFn: (request: { market: WorkspaceMarket; code: string }) => removeFromWatchlist(request.market, request.code),
    onSuccess: (_data, request) => {
      void queryClient.invalidateQueries({ queryKey: marketKey(request.market, 'watchlist') });
      void queryClient.invalidateQueries({ queryKey: marketKey(request.market, 'stock', 'list') });
    },
  });

  // enabled:false 的查询不会被 invalidateQueries 触发，必须走查询自身的 refetch
  const handleScan = () => {
    if (scanStarted) {
      void refetchScan();
    } else {
      setScanStarted(true);
    }
  };

  if (isLoading) {
    return <div className="flex items-center justify-center h-96"><LoadingSpinner size="lg" /></div>;
  }

  if (isError) {
    return (
      <ApiErrorState
        message={apiErrorMessage(error)}
        onRetry={() => refetch()}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold text-text-primary">自选股管理</h1>
        <Button onClick={handleScan} disabled={scanUpdating}>
          {scanUpdating ? '扫描中...' : '信号扫描'}
        </Button>
      </div>

      {/* Add */}
      <Card title="添加自选股">
        <div className="flex flex-wrap items-center gap-3">
          <input
            type="text"
            value={newCode}
            onChange={(e) => setDraft({ ...draft, code: e.target.value })}
            placeholder={market === 'CN' ? 'A股代码，如 600519 / 300750.SZ' : '股票代码，如 AAPL / 00700.HK'}
            className="min-w-0 flex-1 basis-48 rounded border border-border bg-bg-primary px-3 py-1.5 text-sm text-text-primary placeholder-text-muted outline-none focus:border-accent-gold"
          />
          <input
            type="text"
            value={newTags}
            onChange={(e) => setDraft({ ...draft, tags: e.target.value })}
            placeholder="标签（逗号分隔）"
            className="w-48 rounded border border-border bg-bg-primary px-3 py-1.5 text-sm text-text-primary placeholder-text-muted outline-none focus:border-accent-gold"
          />
          <Button onClick={() => addMutation.mutate({ market, code: newCode, tags: newTags })} disabled={!initialized || !newCode.trim() || addMutation.isPending} className="shrink-0">
            添加
          </Button>
        </div>
        {addMutation.variables?.market === market && addMutation.isError && <p role="alert" className="mt-2 text-xs text-accent-red">添加自选失败：{apiErrorMessage(addMutation.error)}</p>}
        {addMutation.variables?.market === market && addMutation.isSuccess && <p role="status" className="mt-2 text-xs text-accent-green">已加入当前市场自选</p>}
        {removeMutation.variables?.market === market && removeMutation.isError && <p role="alert" className="mt-2 text-xs text-accent-red">删除自选失败：{apiErrorMessage(removeMutation.error)}</p>}
      </Card>

      {/* Scan Results */}
      {scanUpdating && scanResult && (
        <div className="text-xs text-accent-gold">数据已更新，正在重新扫描…</div>
      )}
      {scanResult && scanResult.alerts.length > 0 && (
        <Card title={`扫描结果 — ${scanResult.total} 只，${scanResult.alerts.length} 个信号`}>
          <div className="space-y-2">
            {scanResult.alerts.map((a, i) => (
              <div key={i} className="flex items-center gap-3 p-2 rounded bg-bg-hover/50">
                <Badge variant={a.level === 'CRITICAL' ? 'danger' : a.level === 'WARNING' ? 'warning' : 'info'}>
                  {a.level}
                </Badge>
                <span className="text-xs font-mono text-accent-gold">{a.ts_code}</span>
                <span className="text-xs text-text-secondary">{a.alert_type}</span>
                <span className="text-xs text-text-muted flex-1">{a.message}</span>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* List */}
      <Card title={`自选股列表 (${watchlist?.count || 0})`}>
        {!watchlist || watchlist.items.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-10 text-text-muted">
            <div className="text-2xl mb-2 opacity-60">☆</div>
            <div className="text-sm">暂无自选股</div>
            <div className="text-xs mt-1 text-text-muted/70">在上方输入股票代码即可加入观察池</div>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-text-muted">
                  <th className="text-left py-2 px-2">代码</th>
                  <th className="text-left py-2 px-2">名称</th>
                  <th className="text-left py-2 px-2">标签</th>
                  <th className="text-left py-2 px-2">添加日期</th>
                  <th className="text-right py-2 px-2">操作</th>
                </tr>
              </thead>
              <tbody>
                {watchlist.items.map((item) => (
                  <tr key={item.ts_code} className="border-b border-border/50 hover:bg-bg-hover/50">
                    <td className="py-2 px-2 font-mono text-accent-gold">{item.ts_code}</td>
                    <td className="py-2 px-2 text-text-primary">{item.name || '--'}</td>
                    <td className="py-2 px-2">
                      {item.tags ? item.tags.split(',').map((t, i) => (
                        <Badge key={i}>{t.trim()}</Badge>
                      )) : '--'}
                    </td>
                    <td className="py-2 px-2 text-text-muted text-xs">{item.added_date || '--'}</td>
                    <td className="py-2 px-2 text-right space-x-2">
                      <button
                        onClick={() => navigate(`/stock/${item.ts_code}`)}
                        className="text-xs text-accent-blue hover:underline"
                      >
                        分析
                      </button>
                      <button
                        onClick={() => removeMutation.mutate({ market, code: item.ts_code })}
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
        )}
      </Card>
    </div>
  );
}
