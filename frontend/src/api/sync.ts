import api from './client';

/** 与后端 BatchSyncSnapshot 对应的任务快照 */
export interface BatchSyncFailure {
  ts_code: string;
  error: string;
}

export interface MarketSyncInfo {
  oldest_date: string | null;
  latest_date: string | null;
  not_ready: number;
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
  no_data: number;
  indicator_rows: number;
  markets: Record<string, MarketSyncInfo>;
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

/** 单只标的的数据就绪状态 */
export interface StockDataStatus {
  data_date: string | null;
  indicator_date: string | null;
  days: number;
  missing_indicators: number;
  orphan_indicators: number;
  ready: boolean;
  version: string;
}

/** 只读的数据版本与就绪状态（GET /system/data/status） */
export interface DataStatusResponse {
  version: string;
  updating: boolean;
  ready: boolean;
  stocks: Record<string, StockDataStatus>;
  markets: Record<string, MarketSyncInfo>;
}

export async function fetchDataStatus(): Promise<DataStatusResponse> {
  const { data } = await api.get<DataStatusResponse>('/system/data/status');
  return data;
}
