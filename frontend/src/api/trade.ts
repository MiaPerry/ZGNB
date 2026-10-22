import api, { marketRequest } from './client.ts';
import type { WorkspaceMarket } from '../stores/appStore.ts';
import type { TradeList, TradeStats } from './types';

export async function fetchTrades(market: WorkspaceMarket, page = 1, pageSize = 20, signal?: AbortSignal): Promise<TradeList> {
  const { data } = await api.get<TradeList>('/trade/', { params: { page, page_size: pageSize }, ...marketRequest(market, signal) });
  return data;
}

export async function addTrade(market: WorkspaceMarket, text: string, signal?: AbortSignal) {
  const { data } = await api.post('/trade/', { text }, marketRequest(market, signal));
  if (data?.success === false) throw new Error(data.error || '交易解析失败，请补全交易信息');
  return data;
}

export async function parseTrade(market: WorkspaceMarket, text: string, signal?: AbortSignal) {
  const { data } = await api.post('/trade/parse', { text }, marketRequest(market, signal));
  return data;
}

export async function deleteTrade(market: WorkspaceMarket, tradeId: number, signal?: AbortSignal) {
  const { data } = await api.delete(`/trade/${tradeId}`, marketRequest(market, signal));
  return data;
}

export async function fetchTradeStats(market: WorkspaceMarket, signal?: AbortSignal): Promise<TradeStats> {
  const { data } = await api.get<TradeStats>('/trade/stats', marketRequest(market, signal));
  return data;
}
