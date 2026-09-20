import { useEffect, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import {
  fetchBatchSyncStatus,
  startBatchSync,
  type BatchSyncSnapshot,
} from '../api/sync';

const STATUS_KEY = ['batch-sync-status'] as const;

/** 任务进入终态后需要主动刷新的查询（行情相关） */
const REFRESH_KEYS = [['stock'], ['kline'], ['watchlist'], ['sync-status']] as const;

/** 仅标记过期、不自动重跑的查询（需用户主动重新扫描） */
const STALE_ONLY_KEYS = [['screen'], ['watchlist-scan'], ['dashboard-scan']] as const;

export function useBatchSync() {
  const queryClient = useQueryClient();
  // 同一任务只刷新一次，避免轮询重复触发
  const handledTaskRef = useRef<string | null>(null);

  const statusQuery = useQuery({
    queryKey: STATUS_KEY,
    queryFn: fetchBatchSyncStatus,
    // 运行中每 2 秒轮询；空闲时低频检查，便于多页面识别同一任务
    refetchInterval: (query) =>
      query.state.data?.status === 'running' ? 2000 : 30000,
    // 后台标签页也继续轮询，避免切回时按钮停留在“同步中”
    refetchIntervalInBackground: true,
    retry: 1,
  });

  const startMutation = useMutation({
    mutationFn: startBatchSync,
    onSuccess: (snapshot) => {
      queryClient.setQueryData(STATUS_KEY, snapshot);
    },
  });

  const snapshot: BatchSyncSnapshot | undefined =
    startMutation.data ?? statusQuery.data;

  // 任务进入终态（completed / partial_failure / failed）时刷新一次行情相关缓存
  useEffect(() => {
    if (!snapshot?.task_id) return;
    if (snapshot.status === 'running' || snapshot.status === 'idle') return;
    if (handledTaskRef.current === snapshot.task_id) return;
    handledTaskRef.current = snapshot.task_id;

    for (const key of REFRESH_KEYS) {
      void queryClient.invalidateQueries({ queryKey: key });
    }
    for (const key of STALE_ONLY_KEYS) {
      void queryClient.invalidateQueries({ queryKey: key, refetchType: 'none' });
    }
  }, [snapshot, queryClient]);

  // 提交失败（如缺少配置）时提取后端错误信息
  const startError = startMutation.error
    ? axios.isAxiosError(startMutation.error)
      ? String(startMutation.error.response?.data?.detail ?? startMutation.error.message)
      : String(startMutation.error)
    : null;

  return {
    snapshot,
    isRunning: snapshot?.status === 'running',
    start: () => startMutation.mutate(),
    isStarting: startMutation.isPending,
    startError,
    // 状态轮询断网：只提示连接异常，不误判任务结束
    statusUnreachable: statusQuery.isError,
  };
}
