import { useEffect, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import {
  fetchBatchSyncStatus,
  startBatchSync,
} from '../api/sync';
import {
  STATUS_KEY,
  refreshAfterSync,
  shouldHandleTask,
} from '../lib/syncLinkage';

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
      // 提交响应只是启动快照：写入查询缓存后，始终以轮询缓存为唯一状态来源，
      // 避免 running 快照长期遮盖轮询到的终态
      queryClient.setQueryData(STATUS_KEY, snapshot);
    },
  });

  const snapshot = statusQuery.data;

  // 任务进入终态（completed / partial_failure / failed）时联动一次
  useEffect(() => {
    if (!shouldHandleTask(handledTaskRef.current, snapshot)) return;
    handledTaskRef.current = snapshot!.task_id;
    refreshAfterSync(queryClient);
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
