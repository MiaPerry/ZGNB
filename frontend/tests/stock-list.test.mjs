import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseStockListQuery, updateStockListQuery, stockChangeColor } from '../src/lib/stockListQuery.ts';
import { pctColor } from '../src/lib/formatters.ts';

const parse = (query = '') => parseStockListQuery(new URLSearchParams(query));

test('默认浏览美股，URL 可完整恢复搜索、筛选、排序和页码', () => {
  assert.deepEqual(parse(), {
    market: '美股', q: '', industry: undefined, data_status: 'all',
    sort_by: 'ts_code', order: 'asc', page: 1, page_size: 25,
  });
  const state = parse('market=港股&q=李宁&industry=&data_status=available&sort_by=vol&order=desc&page=3&page_size=50');
  assert.equal(state.market, '港股');
  assert.equal(state.q, '李宁');
  assert.equal(state.industry, '');
  assert.equal(state.data_status, 'available');
  assert.equal(state.sort_by, 'vol');
  assert.equal(state.order, 'desc');
  assert.equal(state.page, 3);
  assert.equal(state.page_size, 50);
});

test('非法 URL 参数回退到安全默认值', () => {
  assert.deepEqual(parse('market=主板&sort_by=evil&order=bad&page=-1&page_size=1000&data_status=bad'), parse());
  for (const value of ['1.5', 'Infinity', '1000001', 'NaN']) {
    assert.equal(parse(`page=${value}`).page, 1);
  }
  assert.equal(parse(`q=${'x'.repeat(200)}`).q.length, 100);
});

test('改变筛选或排序回到第一页，翻页保留其他条件', () => {
  const initial = new URLSearchParams('market=美股&q=apple&industry=科技&page=3&sort_by=vol&order=desc');
  const sorted = updateStockListQuery(initial, { sort_by: 'pct_chg' });
  assert.equal(parseStockListQuery(sorted).page, 1);
  assert.equal(sorted.get('industry'), '科技');
  const page = updateStockListQuery(initial, { page: 4 });
  assert.equal(page.get('page'), '4');
  assert.equal(page.get('q'), 'apple');
  assert.equal(initial.get('page'), '3');
});

test('切换市场清除旧行业，未分类与全部行业不混淆', () => {
  const initial = new URLSearchParams('market=美股&industry=科技&page=3');
  const market = updateStockListQuery(initial, { market: '港股' });
  assert.equal(market.has('industry'), false);
  assert.equal(parseStockListQuery(market).page, 1);
  const missing = updateStockListQuery(initial, { industry: '' });
  assert.equal(missing.has('industry'), true);
  assert.equal(missing.get('industry'), '');
  const all = updateStockListQuery(missing, { industry: undefined, q: '' });
  assert.equal(all.has('industry'), false);
  assert.equal(all.has('q'), false);
});

test('股票列表统一使用红涨绿跌的全局样式，零值和缺失值保持中性', () => {
  assert.equal(stockChangeColor(2), 'text-up');
  assert.equal(stockChangeColor(-2), 'text-down');
  assert.equal(stockChangeColor(0), 'text-text-secondary');
  assert.equal(stockChangeColor(null), 'text-text-muted');
});

test('股票列表与详情涨跌配色一致，包括小幅涨跌和正负零', () => {
  for (const value of [20, 0.001, -20, -0.001, 0, -0]) {
    assert.equal(stockChangeColor(value), pctColor(value));
  }
});
