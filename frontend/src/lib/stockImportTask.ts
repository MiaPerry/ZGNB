import type { StockImportSnapshot } from '../api/types';

export const IMPORT_TASK_KEY = ['stock-import-task'] as const;

/** 任务序号及快照修订号由后端给出，拒绝迟到的旧响应。 */
export function mergeImportSnapshot(previous: StockImportSnapshot | undefined, incoming: StockImportSnapshot): StockImportSnapshot {
  if (previous && (previous.sequence > incoming.sequence ||
    (previous.sequence === incoming.sequence && (previous.revision > incoming.revision ||
          (previous.revision === incoming.revision && previous.status !== 'running' && incoming.status === 'running'))))) return previous;
  return incoming;
}

export function importQueryOptions(fetchLatest: () => Promise<StockImportSnapshot>) {
  return {
    queryKey: IMPORT_TASK_KEY,
    queryFn: fetchLatest,
    structuralSharing: (old: unknown, incoming: unknown) =>
      mergeImportSnapshot(old as StockImportSnapshot | undefined, incoming as StockImportSnapshot),
    refetchInterval: (query: { state: { data?: StockImportSnapshot } }) =>
      query.state.data?.status === 'running' ? 2000 : 30000,
    refetchIntervalInBackground: true,
    retry: 1,
  };
}

export function canRetryImport(snapshot: StockImportSnapshot | undefined): boolean {
  return !!snapshot?.task_id && ['failed', 'partial_failure', 'interrupted'].includes(snapshot.status) && snapshot.failed > 0;
}

export const IMPORT_LABELS: Record<string, string> = {
  idle: '尚无任务', running: '正在处理', pending: '等待处理', downloading: '下载并校验',
  indicators: '计算指标', completed: '已完成', skipped: '已存在，跳过',
  partial_failure: '部分失败', failed: '失败', interrupted: '已中断', backup: '备份数据', download: '下载行情', done: '处理结束',
};
