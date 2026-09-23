import type { QueryClient } from '@tanstack/react-query';

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
export function refreshAfterSync(queryClient: QueryClient): void {
  for (const key of REFRESH_KEYS) {
    void queryClient.invalidateQueries({ queryKey: key });
  }
  for (const key of RERUN_KEYS) {
    void queryClient.invalidateQueries({ queryKey: key });
  }
  void queryClient.invalidateQueries({ queryKey: DATA_STATUS_KEY });
}
