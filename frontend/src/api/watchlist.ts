import api, { marketRequest } from './client.ts';
import type { WorkspaceMarket } from '../stores/appStore.ts';
import type { WatchlistList, WatchlistScan } from './types';

export async function fetchWatchlist(market: WorkspaceMarket, signal?: AbortSignal): Promise<WatchlistList> {
  const { data } = await api.get<WatchlistList>('/watchlist/', marketRequest(market, signal));
  return data;
}

export async function addToWatchlist(market: WorkspaceMarket, tsCode: string, tags = '', notes = '', signal?: AbortSignal) {
  const { data } = await api.post('/watchlist/', { ts_code: tsCode, tags, notes }, marketRequest(market, signal));
  return data;
}

export async function removeFromWatchlist(market: WorkspaceMarket, tsCode: string, signal?: AbortSignal) {
  const { data } = await api.delete(`/watchlist/${tsCode}`, marketRequest(market, signal));
  return data;
}

export async function scanWatchlist(market: WorkspaceMarket, signal?: AbortSignal): Promise<WatchlistScan> {
  const { data } = await api.post<WatchlistScan>('/watchlist/scan', null, marketRequest(market, signal));
  return data;
}

export async function fetchReport(market: WorkspaceMarket, signal?: AbortSignal): Promise<{ report: string }> {
  const { data } = await api.get<{ report: string }>('/watchlist/report', marketRequest(market, signal));
  return data;
}
