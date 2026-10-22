import { useState, useRef, useMemo } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useAppStore, normalizeStockCode, isWorkspaceMarket } from '../../stores/appStore';
import type { WorkspaceMarket } from '../../stores/appStore';
import { useQueryClient } from '@tanstack/react-query';
import { marketKey, switchWorkspace } from '../../lib/syncLinkage';
import { cleanStockListQuery, stockDetailDecision } from '../../lib/stockListQuery';
import { useGlobalShortcuts } from '../../lib/hooks';
import SyncButton from './SyncButton';

export default function Header() {
  const navigate = useNavigate();
  const location = useLocation();
  const toggleSidebar = useAppStore((s) => s.toggleSidebar);
  const sidebarCollapsed = useAppStore((s) => s.sidebarCollapsed);
  const searchHistory = useAppStore((s) => s.searchHistory);
  const addSearchHistory = useAppStore((s) => s.addSearchHistory);
  const [input, setInput] = useState('');
  const [historyOpen, setHistoryOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const market = useAppStore((s) => s.currentMarket);
  const initialized = useAppStore((s) => s.marketInitialized);
  const queryClient = useQueryClient();
  const [switching, setSwitching] = useState(false);
  const [message, setMessage] = useState('');

  const handleMarketChange = async (next: WorkspaceMarket) => {
    if (switching || !initialized || next === market) return;
    setSwitching(true);
    try {
      const rejectDetail = stockDetailDecision(location.pathname, next, true) === 'reject';
      const pathname = rejectDetail ? '/stocks' : location.pathname;
      const search = pathname === '/stocks'
        ? cleanStockListQuery(new URLSearchParams(location.search), next, undefined, undefined, true).toString()
        : location.search;
      const changed = await switchWorkspace({
        from: market, to: next,
        dirty: Object.values(useAppStore.getState().marketDrafts).some(Boolean),
        confirm: () => window.confirm('有未提交的交易或添加表单。是否放弃这些修改并切换市场？已提交的后台任务会继续在原市场执行。'),
        cancelReads: (previous) => queryClient.cancelQueries({ queryKey: marketKey(previous) }),
        resetTemporary: (previous) => {
          for (const key of ['screen', 'watchlist-scan', 'dashboard-scan']) queryClient.removeQueries({ queryKey: marketKey(previous, key) });
        },
        commit: (selected) => useAppStore.getState().setCurrentMarket(selected),
        navigate: () => navigate({ pathname, search: search ? (search.startsWith('?') ? search : `?${search}`) : '' }, {
          replace: true, state: rejectDetail ? { marketNotice: `原详情不属于 ${next} 工作空间，已返回当前市场股票库。` } : null,
        }),
      });
      if (changed) { setInput(''); setHistoryOpen(false); setMessage(''); }
    } catch (error) { setMessage(error instanceof Error ? error.message : '市场切换失败'); }
    finally { setSwitching(false); }
  };

  const isDashboard = location.pathname === '/';

  // ⌘K / Ctrl+K 聚焦搜索框
  useGlobalShortcuts(
    useMemo(
      () => [
        {
          key: 'k',
          meta: true,
          handler: () => inputRef.current?.focus(),
        },
        {
          key: 'k',
          ctrl: true,
          handler: () => inputRef.current?.focus(),
        },
      ],
      [],
    ),
  );

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    const code = input.trim();
    if (!code) return;
    try {
      const tsCode = normalizeStockCode(code, market);
      addSearchHistory(tsCode, market);
      navigate(`/stock/${tsCode}`);
      setInput('');
      setHistoryOpen(false);
      setMessage('');
    } catch (error) { setMessage((error as Error).message); }
  };

  return (
    <header className="flex h-14 items-center justify-between border-b border-border/40 bg-bg-secondary/60 backdrop-blur-xl px-6 z-40 sticky top-0">
      <div className="flex items-center gap-3">
        <button
          onClick={toggleSidebar}
          aria-label={sidebarCollapsed ? '展开侧栏' : '收起侧栏'}
          title={sidebarCollapsed ? '展开侧栏' : '收起侧栏'}
          className="text-text-muted hover:text-text-primary transition-colors px-1"
        >
          {sidebarCollapsed ? '☰' : '⟨'}
        </button>
        {/* 全局搜索框:Dashboard 页用 Hero 搜索框,这里隐藏避免重复 */}
        {!isDashboard && (
          <form onSubmit={handleSearch} className="flex items-center gap-2 relative">
            <input
              ref={inputRef}
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onFocus={() => setHistoryOpen(true)}
              onBlur={() => setTimeout(() => setHistoryOpen(false), 150)}
              placeholder={market === 'CN' ? 'A股代码，如 600519 / 300750.SZ (⌘K)' : '美股/港股代码，如 AAPL / 00700.HK (⌘K)'}
              className="w-72 rounded border border-border bg-bg-primary px-3 py-1.5 text-sm text-text-primary placeholder-text-muted outline-none focus:border-accent-gold transition-colors"
            />
            {historyOpen && searchHistory.length > 0 && (
              <div className="absolute top-full mt-1 left-0 w-72 rounded-md border border-border bg-bg-secondary shadow-lg z-50 overflow-hidden">
                <div className="px-3 py-1.5 text-xs text-text-muted border-b border-border/40 flex items-center justify-between">
                  <span>最近查询</span>
                  <button
                    type="button"
                    onMouseDown={(e) => { e.preventDefault(); useAppStore.getState().clearSearchHistory(); }}
                    className="text-text-muted hover:text-accent-red transition-colors"
                  >
                    清除
                  </button>
                </div>
                {searchHistory.slice(0, 6).map((code) => (
                  <button
                    key={code}
                    type="button"
                    onMouseDown={(e) => {
                      e.preventDefault();
                      setInput(code);
                      navigate(`/stock/${code}`);
                      setInput('');
                      setHistoryOpen(false);
                    }}
                    className="w-full text-left px-3 py-1.5 text-sm font-mono text-text-secondary hover:bg-bg-hover hover:text-accent-gold transition-colors"
                  >
                    {code}
                  </button>
                ))}
              </div>
            )}
            <button
              type="submit"
              className="rounded bg-accent-gold/20 px-3 py-1.5 text-sm text-accent-gold hover:bg-accent-gold/30 transition-colors"
            >
              分析
            </button>
          </form>
        )}
      </div>
      <div className="text-xs text-text-muted flex items-center gap-3">
        <SyncButton />
        {!isDashboard && (
          <kbd className="hidden md:inline rounded border border-border/60 bg-bg-primary px-1.5 py-0.5 text-[10px] text-text-muted font-mono">⌘K</kbd>
        )}
        {message && <span role="alert" className="max-w-64 text-accent-red">{message}</span>}
        <select aria-label="工作空间市场" value={market} disabled={!initialized || switching}
          onChange={(event) => { if (isWorkspaceMarket(event.target.value)) void handleMarketChange(event.target.value); }}
          className="rounded border border-border/60 bg-bg-primary px-2 py-1.5 text-xs text-text-primary outline-none focus:border-accent-gold disabled:opacity-50">
          <option value="US">US · 美股</option>
          <option value="CN">CN · A股</option>
        </select>
      </div>
    </header>
  );
}
