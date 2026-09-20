import api from './client';

/** 与后端 BatchSyncSnapshot 对应的任务快照 */
export interface BatchSyncFailure {
  ts_code: string;
  error: string;
}

export interface BatchSyncSnapshot {
  task_id: string | null;
  status: 'idle' | 'running' | 'completed' | 'partial_failure' | 'failed';
  phase: 'idle' | 'backup' | 'sync' | 'indicators' | 'done';
  current_code: string | null;
  total: number;
  processed: number;
  success: number;
  no_change: number;
  failed: number;
  new_rows: number;
  data_date: string | null;
  message: string;
  failures: BatchSyncFailure[];
  started_at: string | null;
  finished_at: string | null;
}

export async function fetchBatchSyncStatus(): Promise<BatchSyncSnapshot> {
  const { data } = await api.get<BatchSyncSnapshot>('/system/sync/batch/status');
  return data;
}

export async function startBatchSync(): Promise<BatchSyncSnapshot> {
  const { data } = await api.post<BatchSyncSnapshot>('/system/sync/batch');
  return data;
}
