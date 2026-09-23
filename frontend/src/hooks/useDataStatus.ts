import { useQuery } from '@tanstack/react-query';
import { fetchDataStatus } from '../api/sync';
import { DATA_STATUS_KEY } from '../lib/syncLinkage';

/**
 * 布局级数据状态观察入口：空闲每 30 秒检查一次数据版本与就绪状态；
 * 批量任务终态时由 useBatchSync 立即触发一次刷新。
 * 多组件共享同一查询键，react-query 自动去重。
 */
export function useDataStatus() {
  return useQuery({
    queryKey: DATA_STATUS_KEY,
    queryFn: fetchDataStatus,
    refetchInterval: 30000,
    staleTime: 10000,
    retry: 1,
  });
}
