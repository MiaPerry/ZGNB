import { useQuery } from '@tanstack/react-query';
import { fetchStockAnalysis, fetchKlineData, fetchCommentary } from '../api/stock';

export function useStockAnalysis(tsCode: string, days = 120) {
  return useQuery({
    queryKey: ['stock', tsCode, days],
    queryFn: () => fetchStockAnalysis(tsCode, days),
    enabled: !!tsCode,
    staleTime: 5 * 60 * 1000,
  });
}

export function useKlineData(tsCode: string, days = 120) {
  return useQuery({
    queryKey: ['kline', tsCode, days],
    queryFn: () => fetchKlineData(tsCode, days),
    enabled: !!tsCode,
    staleTime: 5 * 60 * 1000,
  });
}

export function useCommentary(tsCode: string, days = 120) {
  return useQuery({
    queryKey: ['commentary', tsCode, days],
    queryFn: () => fetchCommentary(tsCode, days),
    enabled: !!tsCode,
    staleTime: 60 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    // AI 点评是付费调用：聚焦、重连、重新挂载均不自动重新生成；失败不自动重试
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchOnMount: false,
    retry: false,
  });
}
