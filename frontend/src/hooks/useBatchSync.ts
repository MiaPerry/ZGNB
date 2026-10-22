import { useEffect, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import {
  fetchBatchSyncStatus,
  startBatchSync,
} from '../api/sync';
import {
  STATUS_KEY,
  marketKey,
  refreshAfterSync,
  shouldHandleTask,
} from '../lib/syncLinkage';

import { useAppStore } from '../stores/appStore';
import type { WorkspaceMarket } from '../stores/appStore';

export function useBatchSync() {
  const queryClient = useQueryClient();
  const market = useAppStore((s) => s.currentMarket);
  const initialized = useAppStore((s) => s.marketInitialized);
  // 同一任务只刷新一次，避免轮询重复触发
  const handledTaskRef = useRef<Partial<Record<WorkspaceMarket, string | null>>>({});

  const statusQuery = useQuery({
    queryKey: marketKey(market, ...STATUS_KEY),
    queryFn: ({ signal }) => fetchBatchSyncStatus(market, signal),
    enabled: initialized,
    // 运行中每 2 秒轮询；空闲时低频检查，便于多页面识别同一任务
    refetchInterval: (query) =>
      query.state.data?.status === 'running' ? 2000 : 30000,
    // 后台标签页也继续轮询，避免切回时按钮停留在“同步中”
    refetchIntervalInBackground: true,
    retry: 1,
  });

  const startMutation = useMutation({
    mutationFn: (request: { market: WorkspaceMarket }) => startBatchSync(request.market),
    onSuccess: (snapshot, request) => {
      // 提交响应只是启动快照：写入查询缓存后，始终以轮询缓存为唯一状态来源，
      // 避免 running 快照长期遮盖轮询到的终态
      queryClient.setQueryData(marketKey(request.market, ...STATUS_KEY), snapshot);
    },
  });

  const snapshot = statusQuery.data;

  // 任务进入终态（completed / partial_failure / failed）时联动一次
  useEffect(() => {
    if (!shouldHandleTask(handledTaskRef.current[market] ?? null, snapshot)) return;
    handledTaskRef.current[market] = snapshot!.task_id;
    refreshAfterSync(queryClient, market);
  }, [snapshot, queryClient, market]);

  // 提交失败（如缺少配置）时提取后端错误信息
  const belongsHere = startMutation.variables?.market === market;
  const startError = belongsHere && startMutation.error
    ? axios.isAxiosError(startMutation.error)
      ? String(startMutation.error.response?.data?.detail ?? startMutation.error.message)
      : String(startMutation.error)
    : null;

  return {
    snapshot,
    isRunning: snapshot?.status === 'running',
    start: () => { if (initialized) startMutation.mutate({ market }); },
    isStarting: belongsHere && startMutation.isPending,
    startError,
    // 状态轮询断网：只提示连接异常，不误判任务结束
    statusUnreachable: statusQuery.isError,
  };
}
