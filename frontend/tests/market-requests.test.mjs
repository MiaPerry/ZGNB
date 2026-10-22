import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as client from '../src/api/client.ts';
import { useAppStore } from '../src/stores/appStore.ts';
import * as stock from '../src/api/stock.ts';
import * as sync from '../src/api/sync.ts';
import * as screen from '../src/api/screen.ts';
import * as watchlist from '../src/api/watchlist.ts';
import * as trade from '../src/api/trade.ts';
import * as backtest from '../src/api/backtest.ts';

test('请求显式捕获市场与 AbortSignal，切换后原配置不变且非法市场拒绝', () => {
  assert.equal(typeof client.marketRequest, 'function');
  const controller = new AbortController();
  const request = client.marketRequest('US', controller.signal);
  useAppStore.getState().setCurrentMarket('CN');
  assert.deepEqual(request.headers, { 'X-Market': 'US' });
  assert.equal(request.signal, controller.signal);
  assert.equal(client.marketRequest('CN').headers['X-Market'], 'CN');
  assert.throws(() => client.marketRequest(undefined));
  assert.throws(() => client.marketRequest('HK'));
});

test('全部股票及同步 API 要求显式市场，并透传 AbortSignal', async () => {
  const oldAdapter = client.default.defaults.adapter;
  const controller = new AbortController();
  const calls = [];
  client.default.defaults.adapter = async (config) => {
    calls.push(config);
    return { data: {}, status: 200, statusText: 'OK', headers: {}, config };
  };
  try {
    const entries = [
      [stock.fetchStockList, [{}]], [stock.startStockImport, ['AAPL.US']],
      [stock.fetchStockImport, ['task-us']], [stock.retryStockImport, ['task-us']],
      [stock.fetchStockAnalysis, ['AAPL.US', 120]], [stock.fetchKlineData, ['AAPL.US', 120]],
      [stock.fetchSignals, ['AAPL.US', 120]], [stock.fetchScore, ['AAPL.US']],
      [stock.fetchCommentary, ['AAPL.US', 120]], [sync.fetchBatchSyncStatus, []],
      [sync.startBatchSync, []], [sync.fetchDataStatus, []],
    ];
    for (const [fn, args] of entries) {
      await fn('US', ...args, controller.signal);
      const config = calls.at(-1);
      assert.equal(config.headers.get('X-Market'), 'US', fn.name);
      assert.equal(config.signal, controller.signal, fn.name);
      const before = calls.length;
      await assert.rejects(() => fn(undefined, ...args, controller.signal));
      assert.equal(calls.length, before, '缺失市场不能发出请求');
    }
  } finally { client.default.defaults.adapter = oldAdapter; }
});

for (const [fn, args] of [
  [screen.fetchStrategies, []], [screen.runScreen, ['B1', 20]],
  [watchlist.fetchWatchlist, []], [watchlist.addToWatchlist, ['600519.SH', '测试', '备注']],
  [watchlist.removeFromWatchlist, ['600519.SH']], [watchlist.scanWatchlist, []], [watchlist.fetchReport, []],
  [trade.fetchTrades, [1, 20]], [trade.addTrade, ['测试记录']], [trade.parseTrade, ['测试文本']],
  [trade.deleteTrade, [1]], [trade.fetchTradeStats, []],
  [backtest.runShaofu, [{ ts_code: '600519.SH' }]], [backtest.runMulti, [{ ts_code: '600519.SH' }]],
  [backtest.runPortfolio, [['600519.SH'], { days: 240 }]],
]) {
  test(`${fn.name}：业务请求固定 CN 并携带取消信号，缺失市场不得发送`, async () => {
    const oldAdapter = client.default.defaults.adapter;
    const controller = new AbortController();
    const calls = [];
    client.default.defaults.adapter = async (config) => {
      calls.push(config);
      return { data: {}, status: 200, statusText: 'OK', headers: {}, config };
    };
    try {
      await fn('CN', ...args, controller.signal);
      assert.equal(calls.at(-1).headers.get('X-Market'), 'CN');
      assert.equal(calls.at(-1).signal, controller.signal);
      await assert.rejects(() => fn(undefined, ...args, controller.signal));
      assert.equal(calls.length, 1);
    } finally { client.default.defaults.adapter = oldAdapter; }
  });
}
