import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as query from '../src/lib/stockListQuery.ts';
import * as state from '../src/stores/appStore.ts';
import { readFileSync } from 'node:fs';

test('CN 默认全部 A股；跨工作空间 URL 清除分类、行业、搜索与页码', () => {
  assert.equal(query.parseStockListQuery(new URLSearchParams(), 'CN').market, '全部A股');
  const old = new URLSearchParams('market=港股&industry=服装&q=李宁&page=7&page_size=50&sort_by=vol&order=desc');
  assert.equal(typeof query.cleanStockListQuery, 'function');
  const clean = query.cleanStockListQuery(old, 'CN');
  for (const key of ['market', 'industry', 'q', 'page']) assert.equal(clean.has(key), false, key);
  assert.equal(clean.get('page_size'), '50');
  assert.equal(clean.get('sort_by'), 'vol');
  assert.equal(clean.get('order'), 'desc');
  assert.equal(old.get('market'), '港股');
});

test('同市场合法 URL 保留；分类/行业依据目标库字典校验，切换仅保留合法排序', () => {
  assert.equal(typeof query.cleanStockListQuery, 'function');
  const good = new URLSearchParams('market=科创板&industry=半导体&q=芯片&page=3&page_size=100&sort_by=pct_chg&order=desc');
  assert.equal(query.cleanStockListQuery(good, 'CN', ['科创板'], ['半导体']).toString(), good.toString());
  const badIndustry = query.cleanStockListQuery(good, 'CN', ['科创板'], ['机械']);
  assert.equal(badIndustry.has('industry'), false);
  assert.equal(badIndustry.has('page'), false);
  const switched = query.cleanStockListQuery(new URLSearchParams('market=科创板&page_size=999&sort_by=evil&order=bad'), 'US', undefined, undefined, true);
  assert.equal(switched.toString(), '');
  assert.equal(query.parseStockListQuery(good, 'CN', ['科创板']).market, '科创板');
});

test('列表先核实对应分类行业字典，再发过滤请求；非法行业清理且合法 URL 不丢失', () => {
  assert.equal(typeof query.stockListRequestGate, 'function');
  const search = new URLSearchParams('market=港股&industry=半导体&q=芯片&page=3&page_size=50&sort_by=pct_chg&order=desc');
  const waiting = query.stockListRequestGate(search, 'US');
  assert.equal(waiting.enabled, false);
  assert.equal(waiting.dictionaryParams.market, '港股');
  assert.equal(waiting.dictionaryParams.industry, undefined);
  assert.equal(waiting.dictionaryParams.q, '');
  assert.equal(waiting.dictionaryParams.page, 1);
  const wrongDictionary = query.stockListRequestGate(search, 'US', ['美股', '港股'], { market: '美股', industries: ['半导体'] });
  assert.equal(wrongDictionary.enabled, false);
  assert.equal(wrongDictionary.cleanSearch, search.toString());
  const invalid = query.stockListRequestGate(search, 'US', ['美股', '港股'], { market: '港股', industries: ['服装'] });
  assert.equal(invalid.enabled, false);
  assert.equal(invalid.params.industry, undefined);
  assert.equal(invalid.params.page, 1);
  assert.equal(invalid.params.q, '芯片');
  const replaced = query.stockListRequestGate(new URLSearchParams(invalid.cleanSearch), 'US', ['美股', '港股'], { market: '港股', industries: ['服装'] });
  assert.equal(replaced.enabled, true);
  assert.equal(replaced.cleanSearch, invalid.cleanSearch);
  const valid = query.stockListRequestGate(search, 'US', ['美股', '港股'], { market: '港股', industries: ['半导体'] });
  assert.equal(valid.enabled, true);
  assert.equal(valid.cleanSearch, search.toString());
  const cn = new URLSearchParams('market=科创板&industry=&page=2');
  assert.equal(query.stockListRequestGate(cn, 'CN', ['科创板'], { market: '科创板', industries: [''] }).enabled, true);
  const crossed = query.stockListRequestGate(search, 'CN', ['科创板'], { market: '全部A股', industries: [] });
  assert.equal(crossed.enabled, false);
  assert.equal(crossed.params.market, '全部A股');
  assert.equal(crossed.params.q, '');
  const source = readFileSync(new URL('../src/pages/StockList.tsx', import.meta.url), 'utf8');
  assert.match(source, /stockListRequestGate/);
  assert.match(source, /fetchStockList\(market,\s*dictionaryParams,\s*signal\)/);
  assert.match(source, /enabled:\s*initialized\s*&&\s*requestGate\.enabled/);
  assert.doesNotMatch(source, /const industryClean = data/);
});

test('搜索按工作空间补后缀并拒绝跨市场代码，不能静默切市场', () => {
  assert.equal(typeof state.normalizeStockCode, 'function');
  for (const [input, market, expected] of [['brk.b', 'US', 'BRK.B.US'], ['aapl', 'US', 'AAPL.US'], ['00700.HK', 'US', '00700.HK'], ['600519', 'CN', '600519.SH'], ['300750', 'CN', '300750.SZ'], ['000300.SH', 'CN', '000300.SH']]) {
    assert.equal(state.normalizeStockCode(input, market), expected);
  }
  for (const [input, market] of [['AAPL', 'CN'], ['600519', 'US'], ['600519.SH', 'US'], ['AAPL.US', 'CN'], ['../AAPL', 'US']]) assert.throws(() => state.normalizeStockCode(input, market));
});

test('首次/刷新/后退详情先校验初始化与市场，错误代码拒绝挂载', () => {
  assert.equal(typeof query.stockDetailDecision, 'function');
  assert.equal(query.stockDetailDecision('/stock/AAPL.US', 'CN', false), 'wait');
  assert.equal(query.stockDetailDecision('/stock/AAPL.US', 'CN', true), 'reject');
  assert.equal(query.stockDetailDecision('/stock/600519.SH', 'US', true), 'reject');
  assert.equal(query.stockDetailDecision('/stock/600519.SH', 'CN', true), 'allow');
  assert.equal(query.stockDetailDecision('/stock/AAPL.US', 'US', true), 'allow');
  assert.equal(query.stockDetailDecision('/stock/%E0', 'US', true), 'reject');
  assert.equal(query.stockDetailDecision('/stocks', 'CN', true), 'allow');
});

test('返回列表状态必须带相同工作空间身份，详情守卫在 Outlet 之前', () => {
  assert.equal(typeof query.stockListReturnTarget, 'function');
  assert.equal(query.stockListReturnTarget({ stockListSearch: 'market=港股', stockListMarket: 'US' }, 'CN'), '/stocks');
  assert.equal(query.stockListReturnTarget({ stockListSearch: 'market=港股' }, 'US'), '/stocks');
  assert.equal(query.stockListReturnTarget({ stockListSearch: 'market=港股&page=2', stockListMarket: 'US' }, 'US'), '/stocks?market=%E6%B8%AF%E8%82%A1&page=2');
  const shell = readFileSync(new URL('../src/components/layout/AppShell.tsx', import.meta.url), 'utf8');
  assert.ok(shell.includes('stockDetailDecision'));
  assert.match(shell, /<Outlet key=\{market\}/);
});
