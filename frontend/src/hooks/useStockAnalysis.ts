import { useQuery } from '@tanstack/react-query';
import { fetchStockAnalysis, fetchKlineData, fetchCommentary } from '../api/stock';
import { useAppStore, codeBelongsToMarket } from '../stores/appStore';
import { marketKey } from '../lib/syncLinkage';

export function useStockAnalysis(tsCode: string, days = 120) {
  const market = useAppStore((s) => s.currentMarket);
  const initialized = useAppStore((s) => s.marketInitialized);
  return useQuery({
    queryKey: marketKey(market, 'stock', tsCode, days),
    queryFn: ({ signal }) => fetchStockAnalysis(market, tsCode, days, signal),
    enabled: initialized && codeBelongsToMarket(tsCode, market),
    staleTime: 5 * 60 * 1000,
  });
}

export function useKlineData(tsCode: string, days = 120) {
  const market = useAppStore((s) => s.currentMarket);
  const initialized = useAppStore((s) => s.marketInitialized);
  return useQuery({
    queryKey: marketKey(market, 'kline', tsCode, days),
    queryFn: ({ signal }) => fetchKlineData(market, tsCode, days, signal),
    enabled: initialized && codeBelongsToMarket(tsCode, market),
    staleTime: 5 * 60 * 1000,
  });
}

export function useCommentary(tsCode: string, days = 120) {
  const market = useAppStore((s) => s.currentMarket);
  const initialized = useAppStore((s) => s.marketInitialized);
  return useQuery({
    queryKey: marketKey(market, 'commentary', tsCode, days),
    queryFn: ({ signal }) => fetchCommentary(market, tsCode, days, signal),
    enabled: initialized && codeBelongsToMarket(tsCode, market),
    staleTime: 60 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    // AI 点评是付费调用：聚焦、重连、重新挂载均不自动重新生成；失败不自动重试
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchOnMount: false,
    retry: false,
  });
}
