import api, { marketRequest } from './client.ts';
import type { WorkspaceMarket } from '../stores/appStore.ts';
import type { BacktestResult } from './types';

export interface BacktestParams {
  ts_code: string;
  days?: number;
  initial_capital?: number;
  stop_loss_pct?: number;
  take_profit_pct?: number;
  position_pct?: number;
}

export async function runShaofu(market: WorkspaceMarket, params: BacktestParams, signal?: AbortSignal): Promise<BacktestResult> {
  const { data } = await api.post<BacktestResult>('/backtest/shaofu', params, marketRequest(market, signal));
  return data;
}

export async function runMulti(market: WorkspaceMarket, params: BacktestParams, signal?: AbortSignal): Promise<BacktestResult> {
  const { data } = await api.post<BacktestResult>('/backtest/multi', params, marketRequest(market, signal));
  return data;
}

export async function runPortfolio(market: WorkspaceMarket, tsCodes: string[], params?: Partial<BacktestParams>, signal?: AbortSignal): Promise<BacktestResult> {
  const { data } = await api.post<BacktestResult>('/backtest/portfolio', {
    ts_codes: tsCodes,
    ...params,
  }, marketRequest(market, signal));
  return data;
}
