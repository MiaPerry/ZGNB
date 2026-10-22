import { useEffect, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { fetchStockImport, retryStockImport, startStockImport } from '../api/stock';
import type { StockImportSnapshot } from '../api/types';
import { IMPORT_TASK_KEY, importQueryOptions, mergeImportSnapshot } from '../lib/stockImportTask';
import { marketKey, refreshAfterSync, shouldHandleTask } from '../lib/syncLinkage';
import { useAppStore } from '../stores/appStore';
import type { WorkspaceMarket } from '../stores/appStore';

export function useStockImport() {
  const client = useQueryClient();
  const market = useAppStore((s) => s.currentMarket);
  const initialized = useAppStore((s) => s.marketInitialized);
  const handled = useRef<Partial<Record<WorkspaceMarket, string | null>>>({});
  const query = useQuery({
    ...importQueryOptions((signal) => fetchStockImport(market, undefined, signal), market),
    enabled: initialized,
  });
  const mutation = useMutation({
    mutationFn: (request: { market: WorkspaceMarket } & ({ codes: string } | { taskId: string })) =>
      'codes' in request ? startStockImport(request.market, request.codes) : retryStockImport(request.market, request.taskId),
    onSuccess: (incoming, request) => {
      const key = marketKey(request.market, ...IMPORT_TASK_KEY);
      client.setQueryData<StockImportSnapshot>(key, (old) => mergeImportSnapshot(old, incoming));
      void client.invalidateQueries({ queryKey: key });
    },
    // 409 时也重新查询，恢复另一个页面启动的任务。
    onError: (_error, request) => { void client.invalidateQueries({ queryKey: marketKey(request.market, ...IMPORT_TASK_KEY) }); },
  });
  const snapshot = query.data;
  useEffect(() => {
    if (!shouldHandleTask(handled.current[market] ?? null, snapshot)) return;
    handled.current[market] = snapshot!.task_id;
    refreshAfterSync(client, market);
  }, [snapshot, client, market]);

  const detail: unknown = axios.isAxiosError(mutation.error) ? mutation.error.response?.data?.detail : null;
  const belongsHere = mutation.variables?.market === market;
  const submitError = belongsHere && mutation.error
    ? typeof detail === 'string' ? detail : '提交失败，请检查代码及连接后重试（每批最多 50 只）'
    : null;
  return {
    snapshot,
    isRunning: snapshot?.status === 'running',
    isSubmitting: belongsHere && mutation.isPending,
    isLoading: query.isLoading,
    statusUnreachable: query.isError,
    submitError,
    start: (codes: string) => { if (initialized) mutation.mutate({ market, codes }); },
    retry: () => { if (initialized && snapshot?.task_id) mutation.mutate({ market, taskId: snapshot.task_id }); },
    refresh: () => { void query.refetch(); },
  };
}
