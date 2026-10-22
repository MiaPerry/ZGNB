import api, { marketRequest } from './client.ts';
import type { WorkspaceMarket } from '../stores/appStore.ts';
import type { StockAnalysis, KlineChart, CommentaryResponse, StockListParams, StockListResponse, StockImportSnapshot } from './types';

export async function fetchStockList(market: WorkspaceMarket, params: StockListParams, signal?: AbortSignal): Promise<StockListResponse> {
  const { data } = await api.get<StockListResponse>('/stock/list', { params, ...marketRequest(market, signal) });
  return data;
}

export async function startStockImport(market: WorkspaceMarket, codes: string, signal?: AbortSignal): Promise<StockImportSnapshot> {
  const { data } = await api.post<StockImportSnapshot>('/stock/imports', { codes }, marketRequest(market, signal));
  return data;
}

export async function fetchStockImport(market: WorkspaceMarket, taskId?: string, signal?: AbortSignal): Promise<StockImportSnapshot> {
  const { data } = await api.get<StockImportSnapshot>(`/stock/imports/${taskId ?? 'latest'}`, marketRequest(market, signal));
  return data;
}

export async function retryStockImport(market: WorkspaceMarket, taskId: string, signal?: AbortSignal): Promise<StockImportSnapshot> {
  const { data } = await api.post<StockImportSnapshot>(`/stock/imports/${taskId}/retry`, null, marketRequest(market, signal));
  return data;
}

export async function fetchStockAnalysis(market: WorkspaceMarket, tsCode: string, days = 120, signal?: AbortSignal): Promise<StockAnalysis> {
  const { data } = await api.get<StockAnalysis>(`/stock/analyze/${tsCode}`, { params: { days }, ...marketRequest(market, signal) });
  return data;
}

export async function fetchKlineData(market: WorkspaceMarket, tsCode: string, days = 120, signal?: AbortSignal): Promise<KlineChart> {
  const { data } = await api.get<KlineChart>(`/stock/analyze/${tsCode}/klines`, { params: { days }, ...marketRequest(market, signal) });
  return data;
}

export async function fetchSignals(market: WorkspaceMarket, tsCode: string, days = 120, signal?: AbortSignal) {
  const { data } = await api.get(`/stock/analyze/${tsCode}/signals`, { params: { days }, ...marketRequest(market, signal) });
  return data;
}

export async function fetchScore(market: WorkspaceMarket, tsCode: string, signal?: AbortSignal) {
  const { data } = await api.get(`/stock/score/${tsCode}`, marketRequest(market, signal));
  return data;
}

export async function fetchCommentary(market: WorkspaceMarket, tsCode: string, days = 120, signal?: AbortSignal): Promise<CommentaryResponse> {
  const { data } = await api.post<CommentaryResponse>(`/commentary/${tsCode}`, null, { params: { days }, timeout: 120000, ...marketRequest(market, signal) });
  return data;
}
