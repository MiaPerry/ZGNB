import type { QueryClient } from '@tanstack/react-query';
import type { WorkspaceMarket } from '../stores/appStore.ts';

/** 所有市场业务缓存共用一个可匹配的隔离前缀。 */
export function marketKey(market: WorkspaceMarket, ...key: readonly unknown[]) {
  if (market !== 'US' && market !== 'CN') throw new Error('缓存缺少有效工作空间市场');
  return ['market', market, ...key] as const;
}

/** 初次核实 CN 前不挂载业务；后台刷新失败不卸载已有表单。 */
export function workspaceContentGate(market: WorkspaceMarket, status: {
  data?: unknown; isPending: boolean; isError: boolean;
}): 'pending' | 'error' | 'allow' {
  if (market === 'CN' && status.data === undefined) {
    if (status.isError) return 'error';
    if (status.isPending) return 'pending';
  }
  return 'allow';
}

/** 切换只取消读取；已经提交的写请求不属于此生命周期。 */
export async function switchWorkspace(options: {
  from: WorkspaceMarket;
  to: WorkspaceMarket;
  dirty: boolean;
  confirm: () => boolean;
  cancelReads: (market: WorkspaceMarket) => Promise<unknown>;
  resetTemporary: (market: WorkspaceMarket) => void;
  commit: (market: WorkspaceMarket) => void;
  navigate: () => void;
}): Promise<boolean> {
  if (options.from === options.to) return false;
  if (options.dirty && !options.confirm()) return false;
  await options.cancelReads(options.from);
  options.resetTemporary(options.from);
  options.commit(options.to);
  options.navigate();
  return true;
}

/** 写响应只允许清空同市场仍与提交值一致的当前表单。 */
export function submittedDraftMatches(
  currentMarket: WorkspaceMarket, submittedMarket: WorkspaceMarket,
  current: readonly string[], submitted: readonly string[],
): boolean {
  return currentMarket === submittedMarket && current.length === submitted.length
    && current.every((value, index) => value === submitted[index]);
}

export interface SubmittedScreen {
  market: WorkspaceMarket;
  strategy: string;
  limit: number;
}

/** 禁用查询仍可被手动 refetch，键与请求必须共同绑定已提交参数。 */
export function screenQueryOptions<T>(
  market: WorkspaceMarket, submitted: SubmittedScreen | null,
  run: (request: SubmittedScreen, signal: AbortSignal) => Promise<T>,
) {
  return {
    queryKey: marketKey(submitted?.market ?? market, 'screen', submitted?.strategy, submitted?.limit),
    queryFn: ({ signal }: { signal: AbortSignal }) => {
      if (!submitted) throw new Error('尚未提交筛选参数');
      return run(submitted, signal);
    },
    enabled: submitted?.market === market,
  };
}

/** 批量同步任务状态查询键（提交与轮询共用的唯一状态来源） */
export const STATUS_KEY = ['batch-sync-status'] as const;

/** 数据版本/就绪状态查询键（布局级低频观察入口） */
export const DATA_STATUS_KEY = ['data-status'] as const;

/** 终态后主动刷新的轻量查询（行情相关） */
export const REFRESH_KEYS = [['stock'], ['kline'], ['watchlist'], ['sync-status']] as const;

/**
 * 终态后自动重跑的扫描查询。
 * 页面在“用户已运行”后才启用对应查询（enabled），未运行的查询保持禁用，
 * 因此这里的 invalidate 只会重跑当前页面已执行过的扫描，不会隐式启动新扫描。
 */
export const RERUN_KEYS = [['screen'], ['watchlist-scan'], ['dashboard-scan']] as const;

export interface SnapshotLike {
  task_id: string | null;
  status: string;
}

/** 只有带任务 ID 的终态快照才触发联动 */
export function isTerminalSnapshot(snapshot: SnapshotLike | null | undefined): boolean {
  if (!snapshot?.task_id) return false;
  return snapshot.status !== 'running' && snapshot.status !== 'idle';
}

/** 同一任务终态只联动一次；新任务允许再次联动 */
export function shouldHandleTask(
  handledTaskId: string | null,
  snapshot: SnapshotLike | null | undefined,
): boolean {
  if (!isTerminalSnapshot(snapshot)) return false;
  return handledTaskId !== snapshot!.task_id;
}

/**
 * 任务终态联动：
 * - 常规行情查询：活跃查询立即刷新，未挂载查询标记过期；
 * - 扫描查询：仅当前页面已运行（启用中）的会被重跑；
 * - 数据状态：立即检查一次新版本。
 */
export function refreshAfterSync(queryClient: QueryClient, market: WorkspaceMarket): void {
  for (const key of REFRESH_KEYS) {
    void queryClient.invalidateQueries({ queryKey: marketKey(market, ...key) });
  }
  for (const key of RERUN_KEYS) {
    void queryClient.invalidateQueries({ queryKey: marketKey(market, ...key) });
  }
  void queryClient.invalidateQueries({ queryKey: marketKey(market, ...DATA_STATUS_KEY) });
}
