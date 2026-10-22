import { create } from 'zustand';

export type WorkspaceMarket = 'US' | 'CN';
type StorageLike = Pick<Storage, 'getItem' | 'setItem'>;

function read(storage: StorageLike | undefined, key: string): string | null {
  try { return storage?.getItem(key) ?? null; } catch { return null; }
}

function write(storage: StorageLike | undefined, key: string, value: string): void {
  try { storage?.setItem(key, value); } catch { /* 存储被禁用时仍保留本标签页内存状态 */ }
}

function browserStorage(kind: 'sessionStorage' | 'localStorage'): StorageLike | undefined {
  try { return typeof window === 'undefined' ? undefined : window[kind]; } catch { return undefined; }
}

export function isWorkspaceMarket(value: unknown): value is WorkspaceMarket {
  return value === 'US' || value === 'CN';
}

export function codeBelongsToMarket(code: string, market: WorkspaceMarket): boolean {
  return market === 'CN' ? /^\d{6}\.(SH|SZ)$/.test(code) : /^[A-Z0-9^][A-Z0-9.^=-]*\.(US|HK)$/.test(code);
}

/** 搜索只补本工作空间后缀，不依据输入自动改变工作空间。 */
export function normalizeStockCode(input: string, market: WorkspaceMarket): string {
  let code = input.trim().toUpperCase();
  if (market === 'CN' && /^[036]\d{5}$/.test(code)) {
    code += code.startsWith('6') ? '.SH' : '.SZ';
  } else if (market === 'US' && /^[A-Z^][A-Z0-9.^=-]*$/.test(code) && !/\.(US|HK|SH|SZ)$/.test(code)) {
    code += '.US';
  }
  if (!codeBelongsToMarket(code, market)) throw new Error(`代码不属于当前 ${market} 工作空间，请检查代码及后缀`);
  return code;
}

function parsed(storage: StorageLike | undefined, key: string): unknown {
  try { return JSON.parse(read(storage, key) ?? 'null'); } catch { return null; }
}

function historyFor(value: unknown, market: WorkspaceMarket): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter((code): code is string => typeof code === 'string' && codeBelongsToMarket(code, market)))];
}

interface AppState {
  currentMarket: WorkspaceMarket;
  marketInitialized: boolean;
  setCurrentMarket: (market: WorkspaceMarket) => void;
  marketDrafts: Record<string, boolean>;
  setMarketDraft: (id: string, dirty: boolean) => void;
  sidebarCollapsed: boolean;
  toggleSidebar: () => void;
  setSidebarCollapsed: (v: boolean) => void;
  searchCode: string;
  setSearchCode: (code: string) => void;
  searchHistory: string[];
  searchHistoryByMarket: Record<WorkspaceMarket, string[]>;
  addSearchHistory: (code: string, market?: WorkspaceMarket) => void;
  clearSearchHistory: (market?: WorkspaceMarket) => void;
  onSharedStorage: (key: string | null) => void;
}

export function createAppStore(
  session = browserStorage('sessionStorage'),
  local = browserStorage('localStorage'),
) {
  const selected = read(session, 'zg-market');
  const shared = read(local, 'zg-default-market');
  const currentMarket = isWorkspaceMarket(selected) ? selected : isWorkspaceMarket(shared) ? shared : 'US';
  // 固定本标签页的启动选择；不覆盖供新标签页使用的共享默认值。
  write(session, 'zg-market', currentMarket);
  const legacy = parsed(local, 'zg-app-store') as { state?: { searchHistory?: unknown; sidebarCollapsed?: boolean } } | null;
  const histories: Record<WorkspaceMarket, string[]> = { US: [], CN: [] };
  for (const market of ['US', 'CN'] as const) {
    const stored = historyFor(parsed(local, `zg-search-history-${market}`), market);
    histories[market] = read(local, 'zg-history-migrated') === '1' ? stored
      : [...new Set([...stored, ...historyFor(legacy?.state?.searchHistory, market)])];
    write(local, `zg-search-history-${market}`, JSON.stringify(histories[market]));
  }
  write(local, 'zg-history-migrated', '1');
  const sidebar = parsed(local, 'zg-sidebar-collapsed');
  return create<AppState>()(
    (set, get) => ({
      searchHistoryByMarket: histories,
      marketDrafts: {},
      setMarketDraft: (id, dirty) => set((s) => {
        const drafts = { ...s.marketDrafts };
        if (dirty) drafts[id] = true;
        else delete drafts[id];
        return { marketDrafts: drafts };
      }),
      onSharedStorage: (key) => {
        if (key === 'zg-sidebar-collapsed') {
          const value = parsed(local, key);
          if (typeof value === 'boolean') set({ sidebarCollapsed: value });
        }
        for (const market of ['US', 'CN'] as const) {
          if (key !== `zg-search-history-${market}`) continue;
          const next = historyFor(parsed(local, key), market);
          set((s) => ({ searchHistoryByMarket: { ...s.searchHistoryByMarket, [market]: next },
            ...(s.currentMarket === market ? { searchHistory: next } : {}) }));
        }
      },
      currentMarket,
      marketInitialized: true,
      setCurrentMarket: (market) => {
        if (!isWorkspaceMarket(market)) throw new Error('无效工作空间市场');
        write(session, 'zg-market', market);
        write(local, 'zg-default-market', market);
        const history = historyFor(parsed(local, `zg-search-history-${market}`) ?? get().searchHistoryByMarket[market], market);
        set((s) => ({ currentMarket: market, searchCode: '', marketDrafts: {}, searchHistory: history,
          searchHistoryByMarket: { ...s.searchHistoryByMarket, [market]: history } }));
      },
      sidebarCollapsed: typeof sidebar === 'boolean' ? sidebar : legacy?.state?.sidebarCollapsed === true,
      toggleSidebar: () => get().setSidebarCollapsed(!get().sidebarCollapsed),
      setSidebarCollapsed: (v) => {
        write(local, 'zg-sidebar-collapsed', JSON.stringify(v));
        set({ sidebarCollapsed: v });
      },
      searchCode: '',
      setSearchCode: (code) => set({ searchCode: code }),
      searchHistory: histories[currentMarket],
      addSearchHistory: (code, market = get().currentMarket) => {
        if (!codeBelongsToMarket(code, market)) throw new Error('代码与工作空间市场不符');
        const sharedHistory = historyFor(parsed(local, `zg-search-history-${market}`) ?? get().searchHistoryByMarket[market], market);
        const next = [code, ...sharedHistory.filter((c) => c !== code)];
        write(local, `zg-search-history-${market}`, JSON.stringify(next));
        set((s) => ({ searchHistoryByMarket: { ...s.searchHistoryByMarket, [market]: next },
          ...(s.currentMarket === market ? { searchHistory: next } : {}) }));
      },
      clearSearchHistory: (market = get().currentMarket) => {
        write(local, `zg-search-history-${market}`, '[]');
        set((s) => ({ searchHistoryByMarket: { ...s.searchHistoryByMarket, [market]: [] },
          ...(s.currentMarket === market ? { searchHistory: [] } : {}) }));
      },
    }),
  );
}

export const useAppStore = createAppStore();

if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event) => {
    if (event.storageArea === browserStorage('localStorage')) useAppStore.getState().onSharedStorage(event.key);
  });
}
