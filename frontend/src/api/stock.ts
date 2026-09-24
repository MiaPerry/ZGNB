import api from './client';
import type { StockAnalysis, KlineChart, CommentaryResponse, StockListParams, StockListResponse, StockImportSnapshot } from './types';

export async function fetchStockList(params: StockListParams, signal?: AbortSignal): Promise<StockListResponse> {
  const { data } = await api.get<StockListResponse>('/stock/list', { params, signal });
  return data;
}

export async function startStockImport(codes: string): Promise<StockImportSnapshot> {
  const { data } = await api.post<StockImportSnapshot>('/stock/imports', { codes });
  return data;
}

export async function fetchStockImport(taskId?: string): Promise<StockImportSnapshot> {
  const { data } = await api.get<StockImportSnapshot>(`/stock/imports/${taskId ?? 'latest'}`);
  return data;
}

export async function retryStockImport(taskId: string): Promise<StockImportSnapshot> {
  const { data } = await api.post<StockImportSnapshot>(`/stock/imports/${taskId}/retry`);
  return data;
}

export async function fetchStockAnalysis(tsCode: string, days = 120): Promise<StockAnalysis> {
  const { data } = await api.get<StockAnalysis>(`/stock/analyze/${tsCode}`, { params: { days } });
  return data;
}

export async function fetchKlineData(tsCode: string, days = 120): Promise<KlineChart> {
  const { data } = await api.get<KlineChart>(`/stock/analyze/${tsCode}/klines`, { params: { days } });
  return data;
}

export async function fetchSignals(tsCode: string, days = 120) {
  const { data } = await api.get(`/stock/analyze/${tsCode}/signals`, { params: { days } });
  return data;
}

export async function fetchScore(tsCode: string) {
  const { data } = await api.get(`/stock/score/${tsCode}`);
  return data;
}

export async function fetchCommentary(tsCode: string, days = 120): Promise<CommentaryResponse> {
  const { data } = await api.post<CommentaryResponse>(`/commentary/${tsCode}`, null, { params: { days }, timeout: 120000 });
  return data;
}
