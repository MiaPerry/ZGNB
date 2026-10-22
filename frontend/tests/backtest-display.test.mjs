import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as display from '../src/lib/formatters.ts';
import { readFileSync } from 'node:fs';

test('模拟指标 null/非有限值显示不可计算，真实 0 不被当成缺失', () => {
  assert.equal(typeof display.formatBacktestMetric, 'function');
  assert.equal(display.formatBacktestMetric(null), '不可计算');
  assert.equal(display.formatBacktestMetric(Infinity), '不可计算');
  assert.equal(display.formatBacktestMetric(NaN), '不可计算');
  assert.equal(display.formatBacktestMetric(0, 'return'), '+0.00%');
  assert.equal(display.formatBacktestMetric(0, 'win_rate'), '0.0%');
  assert.equal(display.formatBacktestMetric(0, 'drawdown'), '+0.00%');
  assert.equal(display.formatBacktestMetric(1.25), '1.25');
  assert.equal(display.formatBacktestMetric(0.5, 'win_rate'), '50.0%');
  assert.equal(display.formatBacktestMetric(10, 'drawdown'), '-10.00%');
});

test('回测页面接入模拟元数据、字段级原因、参考价与净值标签', () => {
  const page = readFileSync(new URL('../src/pages/Backtest.tsx', import.meta.url), 'utf8');
  for (const text of ['strategy_simulation', '策略模拟净值', '前复权参考价', '模拟进场', '模拟出场', 'metric_unavailable_reasons', 'assumptions', 'formatBacktestMetric']) {
    assert.ok(page.includes(text), `页面缺少 ${text}`);
  }
  assert.ok(page.includes('request.market'), '回调必须绑定提交市场');
});
