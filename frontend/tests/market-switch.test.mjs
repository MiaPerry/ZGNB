import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createAppStore } from '../src/stores/appStore.ts';
import * as linkage from '../src/lib/syncLinkage.ts';
import { readFileSync } from 'node:fs';

test('未提交草稿单独登记，切换取消没有存储、路由或请求副作用', async () => {
  const store = createAppStore();
  assert.equal(typeof store.getState().setMarketDraft, 'function');
  assert.equal(typeof linkage.switchWorkspace, 'function');
  store.getState().setMarketDraft('trade-form', true);
  const effects = [];
  const changed = await linkage.switchWorkspace({ from: 'US', to: 'CN', dirty: Object.values(store.getState().marketDrafts).some(Boolean),
    confirm: () => false, cancelReads: async () => effects.push('cancel'), resetTemporary: () => effects.push('reset'),
    commit: (market) => { effects.push('commit'); store.getState().setCurrentMarket(market); }, navigate: () => effects.push('navigate') });
  assert.equal(changed, false);
  assert.deepEqual(effects, []);
  assert.equal(store.getState().currentMarket, 'US');
  assert.equal(store.getState().marketDrafts['trade-form'], true);
});

test('确认后先取消旧读再切换/replace，已提交写任务不被取消或重发', async () => {
  assert.equal(typeof linkage.switchWorkspace, 'function');
  const effects = [];
  let resolveWrite;
  const write = new Promise((resolve) => { resolveWrite = resolve; });
  const changed = await linkage.switchWorkspace({ from: 'US', to: 'CN', dirty: true,
    confirm: () => { effects.push('confirm'); return true; }, cancelReads: async (market) => effects.push(`cancel-${market}`),
    resetTemporary: (market) => effects.push(`reset-${market}`), commit: (market) => effects.push(`commit-${market}`), navigate: () => effects.push('replace') });
  assert.equal(changed, true);
  assert.deepEqual(effects, ['confirm', 'cancel-US', 'reset-US', 'commit-CN', 'replace']);
  resolveWrite('US');
  assert.equal(await write, 'US');
  const header = readFileSync(new URL('../src/components/layout/Header.tsx', import.meta.url), 'utf8');
  assert.match(header, /<select[^>]*aria-label="工作空间市场"/);
  assert.ok(header.includes('switchWorkspace'));
  assert.ok(header.includes('replace: true'));
  for (const file of ['pages/Trades.tsx', 'pages/Watchlist.tsx', 'components/stock/StockImportDialog.tsx']) {
    assert.ok(readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8').includes('useMarketDraft'), `${file} 未登记草稿`);
  }
});
