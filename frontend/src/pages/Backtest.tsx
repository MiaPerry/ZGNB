import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import axios from 'axios';
import { useAppStore, normalizeStockCode } from '../stores/appStore';
import type { WorkspaceMarket } from '../stores/appStore';
import { marketKey } from '../lib/syncLinkage';
import { runShaofu } from '../api/backtest';
import { useDataStatus } from '../hooks/useDataStatus';
import type { BacktestResult } from '../api/types';
import Card from '../components/ui/Card';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import EquityCurveChart from '../components/charts/EquityCurveChart';
import { formatAssetPrice, formatPct, formatBacktestMetric } from '../lib/formatters';

export default function Backtest() {
  const market = useAppStore((s) => s.currentMarket);
  const initialized = useAppStore((s) => s.marketInitialized);
  const [tsCode, setTsCode] = useState('');
  const [inputError, setInputError] = useState('');
  const [days, setDays] = useState(250);
  const [storedResult, setResult] = useState<BacktestResult | null>(null);
  const result = storedResult?.workspace_market === market ? storedResult : null;
  const isSimulation = result?.simulation_mode === 'strategy_simulation';
  const { data: dataStatus } = useDataStatus();

  const mutation = useMutation({
    mutationKey: marketKey(market, 'backtest'),
    mutationFn: (request: { market: WorkspaceMarket; code: string; days: number }) =>
      runShaofu(request.market, { ts_code: request.code, days: request.days }),
    onSuccess: (data, request) => {
      if (useAppStore.getState().currentMarket === request.market && data.workspace_market === request.market) setResult(data);
    },
  });
  const isPending = mutation.variables?.market === market && mutation.isPending;
  const error = mutation.variables?.market === market ? mutation.error : null;
  const errorText = axios.isAxiosError(error) ? String(error.response?.data?.detail ?? error.message) : error?.message;

  // 回测结果绑定生成时的数据版本；行情更新后仅提示，由用户手动重跑（重任务）
  const currentVersion = result?.data_version
    ? (dataStatus?.stocks?.[result.ts_code]?.version ?? dataStatus?.version)
    : undefined;
  const isStale = Boolean(
    result?.data_version && currentVersion && currentVersion !== result.data_version
  );

  const handleRun = () => {
    if (!initialized || !tsCode.trim()) return;
    try {
      const code = normalizeStockCode(tsCode, market);
      setTsCode(code);
      setInputError('');
      mutation.mutate({ market, code, days });
    } catch (error) { setInputError((error as Error).message); }
  };

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold text-text-primary">策略回测</h1>

      {/* Config */}
      <Card title="回测配置">
        <div className="flex items-center gap-4">
          <div className="flex-1">
            <label className="block text-xs text-text-muted mb-1">股票代码</label>
            <input
              type="text"
              value={tsCode}
              onChange={(e) => setTsCode(e.target.value)}
              placeholder={market === 'CN' ? '如 600519 / 300750.SZ' : '如 AAPL / BRK.B.US'}
              className="w-full rounded border border-border bg-bg-primary px-3 py-1.5 text-sm text-text-primary placeholder-text-muted outline-none focus:border-accent-gold"
            />
          </div>
          <div className="w-32">
            <label className="block text-xs text-text-muted mb-1">回测天数</label>
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              className="w-full rounded border border-border bg-bg-primary px-2 py-1.5 text-sm text-text-primary"
            >
              {[120, 250, 365, 500, 730].map((n) => (
                <option key={n} value={n}>{n} 天</option>
              ))}
            </select>
          </div>
          <div className="flex items-end gap-3">
            <button
              onClick={handleRun}
              disabled={!initialized || isPending || !tsCode.trim()}
              className="rounded-lg border border-accent-gold/40 bg-gradient-to-b from-accent-gold/30 to-accent-gold/15 px-5 py-2 text-sm font-bold tracking-wider text-accent-gold shadow-[0_0_20px_-8px_rgba(245,158,11,0.5)] transition-all hover:from-accent-gold/40 hover:to-accent-gold/25 hover:shadow-[0_0_24px_-6px_rgba(245,158,11,0.7)] disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none"
            >
              {isPending ? '回测中...' : '▶ 开始回测'}
            </button>
          </div>
        </div>
      </Card>

      {/* Loading */}
      {isPending && (
        <div className="flex items-center justify-center py-16">
          <LoadingSpinner size="lg" />
        </div>
      )}

      {/* Results */}
      {(inputError || errorText) && <p role="alert" className="text-sm text-accent-red">{inputError || errorText}</p>}
      {result && !isPending && (
        <>
          {isStale && (
            <div className="rounded-lg border border-accent-gold/30 bg-accent-gold/[0.06] px-4 py-2 text-xs text-accent-gold">
              结果基于 {result.data_date || '历史'} 的数据，行情已更新；点击“开始回测”重新运行。
            </div>
          )}
          {isSimulation && (
            <Card title="策略模拟 · 前复权参考价">
              <ul className="space-y-1 text-xs text-text-muted">
                {result.assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}
              </ul>
            </Card>
          )}
          {/* Summary Cards */}
          <div className="grid grid-cols-6 gap-3">
            {[
              { key: 'total_return', label: '总收益', value: formatBacktestMetric(result.summary.total_return, 'return'), color: result.summary.total_return === null ? '#64748b' : result.summary.total_return >= 0 ? '#ef4444' : '#22c55e' },
              { key: 'win_rate', label: '胜率', value: formatBacktestMetric(result.summary.win_rate, 'win_rate'), color: '#f59e0b' },
              { key: 'profit_factor', label: '盈亏比', value: formatBacktestMetric(result.summary.profit_factor), color: '#3b82f6' },
              { key: 'max_drawdown', label: '最大回撤', value: formatBacktestMetric(result.summary.max_drawdown, 'drawdown'), color: '#ef4444' },
              { key: 'sharpe_ratio', label: '夏普比率', value: formatBacktestMetric(result.summary.sharpe_ratio), color: '#a855f7' },
              { key: 'total_trades', label: '交易次数', value: String(result.summary.total_trades), color: '#06b6d4' },
            ].map((item) => (
              <Card key={item.label}>
                <div className="text-center">
                  <div className="text-xs text-text-muted">{item.label}</div>
                  <div className="text-lg font-bold mt-1" style={{ color: item.color }} title={result.metric_unavailable_reasons[item.key]}>{item.value}</div>
                  {result.metric_unavailable_reasons[item.key] && <p className="mt-1 text-[10px] text-text-muted">{result.metric_unavailable_reasons[item.key]}</p>}
                </div>
              </Card>
            ))}
          </div>

          {/* Equity Curve */}
          <Card title={isSimulation ? '策略模拟净值（初始基值 100）' : '资金曲线'}>
            {result.equity_curve.length === 0 ? <p className="py-8 text-center text-sm text-text-muted">{result.metric_unavailable_reasons.equity_curve || '暂无数据'}</p>
              : <EquityCurveChart equityCurve={result.equity_curve} seriesLabel={isSimulation ? '策略模拟净值（基值100）' : '资金曲线'} height={300} />}
          </Card>

          {/* Trade Table */}
          <Card title={`${isSimulation ? '模拟进出场明细 · 前复权参考价' : '交易明细'} (${result.trades.length} 笔)`}>
            <div className="overflow-x-auto max-h-96 overflow-y-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-bg-card">
                  <tr className="border-b border-border text-text-muted">
                    <th className="text-left py-1.5 px-2">{isSimulation ? '模拟进场日期' : '买入日期'}</th>
                    <th className="text-right py-1.5 px-2">{isSimulation ? '进场前复权参考价' : '买入价'}</th>
                    <th className="text-left py-1.5 px-2">{isSimulation ? '模拟出场日期' : '卖出日期'}</th>
                    <th className="text-right py-1.5 px-2">{isSimulation ? '出场前复权参考价' : '卖出价'}</th>
                    <th className="text-right py-1.5 px-2">盈亏</th>
                    <th className="text-right py-1.5 px-2">持仓天数</th>
                    <th className="text-left py-1.5 px-2">退出原因</th>
                  </tr>
                </thead>
                <tbody>
                  {result.trades.map((t, i) => (
                    <tr key={i} className="border-b border-border/50">
                      <td className="py-1.5 px-2 text-text-secondary">{t.entry_date}</td>
                      <td className="py-1.5 px-2 text-right font-mono">{formatAssetPrice(t.entry_price, market, result.ts_code)}</td>
                      <td className="py-1.5 px-2 text-text-secondary">{t.exit_date || '--'}</td>
                      <td className="py-1.5 px-2 text-right font-mono">{formatAssetPrice(t.exit_price, market, result.ts_code)}</td>
                      <td className={`py-1.5 px-2 text-right font-mono font-bold ${t.pnl_pct >= 0 ? 'text-up' : 'text-down'}`}>
                        {formatPct(t.pnl_pct)}
                      </td>
                      <td className="py-1.5 px-2 text-right">{t.holding_days}</td>
                      <td className="py-1.5 px-2 text-text-muted">{t.exit_reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
