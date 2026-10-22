import api, { marketRequest } from './client.ts';
import type { WorkspaceMarket } from '../stores/appStore.ts';
import type { ScreenResult, StrategyInfo } from './types';

export async function fetchStrategies(market: WorkspaceMarket, signal?: AbortSignal): Promise<StrategyInfo[]> {
  const { data } = await api.get<StrategyInfo[]>('/screen/strategies', marketRequest(market, signal));
  return data;
}

export async function runScreen(market: WorkspaceMarket, strategy: string, limit = 20, signal?: AbortSignal): Promise<ScreenResult> {
  const { data } = await api.post<ScreenResult>('/screen/run', { strategy, limit, use_parallel: true }, marketRequest(market, signal));
  return data;
}
