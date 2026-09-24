import { useEffect, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { fetchStockImport, retryStockImport, startStockImport } from '../api/stock';
import type { StockImportSnapshot } from '../api/types';
import { IMPORT_TASK_KEY, importQueryOptions, mergeImportSnapshot } from '../lib/stockImportTask';
import { refreshAfterSync, shouldHandleTask } from '../lib/syncLinkage';

export function useStockImport() {
  const client = useQueryClient();
  const handled = useRef<string | null>(null);
  const query = useQuery(importQueryOptions(() => fetchStockImport()));
  const mutation = useMutation({
    mutationFn: (request: { codes: string } | { taskId: string }) =>
      'codes' in request ? startStockImport(request.codes) : retryStockImport(request.taskId),
    onSuccess: (incoming) => {
      client.setQueryData<StockImportSnapshot>(IMPORT_TASK_KEY, (old) => mergeImportSnapshot(old, incoming));
      void client.invalidateQueries({ queryKey: IMPORT_TASK_KEY });
    },
    // 409 时也重新查询，恢复另一个页面启动的任务。
    onError: () => { void client.invalidateQueries({ queryKey: IMPORT_TASK_KEY }); },
  });
  const snapshot = query.data;
  useEffect(() => {
    if (!shouldHandleTask(handled.current, snapshot)) return;
    handled.current = snapshot!.task_id;
    refreshAfterSync(client);
  }, [snapshot, client]);

  const detail: unknown = axios.isAxiosError(mutation.error) ? mutation.error.response?.data?.detail : null;
  const submitError = mutation.error
    ? typeof detail === 'string' ? detail : '提交失败，请检查代码及连接后重试（每批最多 50 只）'
    : null;
  return {
    snapshot,
    isRunning: snapshot?.status === 'running',
    isSubmitting: mutation.isPending,
    isLoading: query.isLoading,
    statusUnreachable: query.isError,
    submitError,
    start: (codes: string) => mutation.mutate({ codes }),
    retry: () => { if (snapshot?.task_id) mutation.mutate({ taskId: snapshot.task_id }); },
    refresh: () => { void query.refetch(); },
  };
}
