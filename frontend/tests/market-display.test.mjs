import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as display from '../src/lib/formatters.ts';
import * as client from '../src/api/client.ts';
import { readFileSync } from 'node:fs';
import * as tradeApi from '../src/api/trade.ts';
import * as linkage from '../src/lib/syncLinkage.ts';

 test('价格根据工作空间、标的币种与显式资产身份展示；CN 指数不是人民币金额', () => {
  assert.equal(typeof display.assetPriceUnit, 'function');
  assert.equal(typeof display.formatAssetPrice, 'function');
  assert.equal(display.assetPriceUnit('CN', '600519.SH', 'stock', 'CNY'), 'CNY');
  assert.equal(display.assetPriceUnit('CN', '600000.SH', 'index', 'CNY'), '点');
  assert.equal(display.assetPriceUnit('US', 'AAPL.US'), 'USD');
  assert.equal(display.assetPriceUnit('US', '00700.HK'), 'HKD');
  assert.equal(display.assetPriceUnit('US', '^GSPC.US', 'index', 'USD'), '点');
  assert.equal(display.formatAssetPrice(0, 'CN', '600519.SH', 'stock'), '0.00 CNY');
  assert.equal(display.formatAssetPrice(null, 'US', 'AAPL.US'), '--');
});

test('CN 尚未初始化是明确错误而非零值成功；全站入口在展示业务前保护', () => {
  assert.equal(typeof client.apiErrorMessage, 'function');
  const error = { isAxiosError: true, message: 'Request failed with status code 503', response: { data: { error: 'market_unavailable', detail: 'CN尚未初始化' } } };
  assert.equal(client.apiErrorMessage(error), 'CN尚未初始化');
  const shell = readFileSync(new URL('../src/components/layout/AppShell.tsx', import.meta.url), 'utf8');
  assert.ok(shell.includes('dataStatus.isError'));
  assert.ok(shell.includes('apiErrorMessage'));
});

test('列表、搜索、分析、图表、真实交易和添加弹窗均接入所属市场', () => {
  const file = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8');
  assert.ok(file('pages/StockList.tsx').includes('stockListRequestGate'));
  assert.ok(file('pages/StockList.tsx').includes('stockListMarket: market'));
  assert.ok(file('pages/StockList.tsx').includes('formatAssetPrice'));
  assert.ok(file('pages/StockAnalysis.tsx').includes('stockListReturnTarget'));
  assert.ok(file('pages/StockAnalysis.tsx').includes('formatAssetPrice'));
  assert.ok(file('pages/Dashboard.tsx').includes('normalizeStockCode'));
  assert.ok(file('pages/Backtest.tsx').includes('normalizeStockCode'));
  assert.ok(file('pages/Trades.tsx').includes('formatAssetPrice'));
  assert.ok(file('components/charts/KlineChart.tsx').includes('priceUnit'));
  assert.ok(file('components/charts/EquityCurveChart.tsx').includes('seriesLabel'));
  assert.ok(file('components/stock/StockImportDialog.tsx').includes('A股代码'));
});

test('已确认缺库立即进入错误态，隐藏标签页不等待暂停重试；临时错误仍重试一次', async () => {
  assert.equal(typeof client.shouldRetryDataStatus, 'function');
  const unavailable = { isAxiosError: true, response: { status: 503, data: { error: 'market_unavailable', detail: 'CN尚未初始化' } } };
  assert.equal(client.shouldRetryDataStatus(0, unavailable), false);
  assert.equal(client.shouldRetryDataStatus(0, new Error('网络失败')), true);
  assert.equal(client.shouldRetryDataStatus(1, new Error('网络失败')), false);
  const { QueryClient, focusManager } = await import('@tanstack/react-query');
  const queryClient = new QueryClient();
  focusManager.setFocused(false);
  let calls = 0;
  let timeout;
  try {
    const request = queryClient.fetchQuery({ queryKey: ['market', 'CN', 'missing-status'],
      queryFn: () => { calls++; throw unavailable; }, retry: client.shouldRetryDataStatus, retryDelay: 0 });
    await assert.rejects(Promise.race([request, new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error('缺库错误仍在等待隐藏标签页重试')), 100);
    })]), (error) => error === unavailable);
    assert.equal(calls, 1);
    assert.equal(queryClient.getQueryState(['market', 'CN', 'missing-status']).status, 'error');
  } finally {
    clearTimeout(timeout);
    queryClient.clear();
    focusManager.setFocused(undefined);
  }
  const hook = readFileSync(new URL('../src/hooks/useDataStatus.ts', import.meta.url), 'utf8');
  assert.match(hook, /retry:\s*shouldRetryDataStatus/);
});

test('CN 初次核实失败阻止挂载；后台状态刷新失败保留业务及草稿', () => {
  assert.equal(typeof linkage.workspaceContentGate, 'function');
  assert.equal(linkage.workspaceContentGate('CN', { isPending: true, isError: false }), 'pending');
  assert.equal(linkage.workspaceContentGate('CN', { isPending: false, isError: true }), 'error');
  assert.equal(linkage.workspaceContentGate('CN', { isPending: false, isError: true, data: { ready: true } }), 'allow');
  assert.equal(linkage.workspaceContentGate('US', { isPending: true, isError: false }), 'allow');
  const shell = readFileSync(new URL('../src/components/layout/AppShell.tsx', import.meta.url), 'utf8');
  assert.ok(shell.includes('workspaceContentGate'));
  assert.ok(shell.includes('数据状态刷新失败'));
});

test('真实交易解析失败不得触发保存成功；原市场表单保留并显示具体原因', async () => {
  const original = client.default.post;
  client.default.post = async () => ({ data: { success: false, error: '缺少交易价格', missing_fields: ['price'] } });
  try {
    await assert.rejects(() => tradeApi.addTrade('CN', '买入茅台'), /缺少交易价格/);
  } finally {
    client.default.post = original;
  }
});

test('自选添加输入标准化且保持原始草稿匹配，所有写错误可见', () => {
  const watch = readFileSync(new URL('../src/pages/Watchlist.tsx', import.meta.url), 'utf8');
  const trades = readFileSync(new URL('../src/pages/Trades.tsx', import.meta.url), 'utf8');
  assert.ok(watch.includes('normalizeStockCode(request.code, request.market)'));
  assert.ok(watch.includes('[current.code, current.tags], [request.code, request.tags]'));
  assert.ok(watch.includes('添加自选失败'));
  assert.ok(watch.includes('删除自选失败'));
  assert.ok(trades.includes('apiErrorMessage(addMutation.error)'));
  assert.ok(trades.includes('apiErrorMessage(deleteMutation.error)'));
  assert.ok(trades.includes('!addMutation.isPending'));
});

test('点评数字不被猜测成人民币，保留源单位且安全转义；指标价格有单位', () => {
  assert.equal(typeof display.renderCommentaryInline, 'function');
  const html = display.renderCommentaryInline('参考价 95.39 USD，指数 102.40 点，**关键**，涨幅 +5.27% <img src=x onerror=alert(1)>');
  assert.ok(!html.includes('¥'));
  assert.ok(html.includes('USD'));
  assert.ok(html.includes('点'));
  assert.ok(html.includes('<strong'));
  assert.ok(html.includes('&lt;img'));
  assert.ok(!html.includes('<img'));
  const card = readFileSync(new URL('../src/components/stock/CommentaryCard.tsx', import.meta.url), 'utf8');
  const indicator = readFileSync(new URL('../src/components/stock/IndicatorPanel.tsx', import.meta.url), 'utf8');
  assert.ok(card.includes('renderCommentaryInline(section.body)'));
  assert.ok(card.includes('priceUnit'));
  assert.ok(indicator.includes('priceUnit'));
});

test('真实交易统计保留既有数值；US混合原币不冒充统一币种收益', () => {
  assert.equal(typeof display.formatTradeTotal, 'function');
  assert.equal(display.formatTradeTotal(123.45, 'US'), '123.45（原币数值，未换算）');
  assert.equal(display.formatTradeTotal(0, 'CN'), '0.00 CNY');
  assert.equal(display.formatTradeTotal(undefined, 'CN'), '--');
  const trades = readFileSync(new URL('../src/pages/Trades.tsx', import.meta.url), 'utf8');
  assert.ok(trades.includes('formatTradeTotal'));
  assert.ok(!trades.includes("'未做跨币种汇总'"));
});
