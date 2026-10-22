import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as app from '../src/stores/appStore.ts';

function memoryStorage(values = {}) {
  const data = new Map(Object.entries(values));
  return {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, value),
  };
}

test('启动市场：本标签页优先、共享默认其次、无有效值使用 US', () => {
  assert.equal(typeof app.createAppStore, 'function');
  for (const [session, shared, expected] of [
    ['US', 'CN', 'US'], ['CN', 'US', 'CN'], [null, 'CN', 'CN'], ['bad', 'CN', 'CN'],
    [null, null, 'US'], ['bad', 'bad', 'US'],
  ]) {
    const store = app.createAppStore(memoryStorage({ 'zg-market': session }), memoryStorage({ 'zg-default-market': shared }));
    assert.equal(store.getState().currentMarket, expected);
    assert.equal(store.getState().marketInitialized, true);
  }
});

test('显式切换只改变本标签页；新标签页采用共享默认，刷新保留自己的市场', () => {
  const local = memoryStorage();
  const sessionA = memoryStorage();
  const sessionB = memoryStorage();
  const a = app.createAppStore(sessionA, local);
  const b = app.createAppStore(sessionB, local);
  assert.equal(typeof a.getState().setCurrentMarket, 'function');
  a.getState().setCurrentMarket('CN');
  assert.equal(local.getItem('zg-default-market'), 'CN');
  assert.equal(sessionA.getItem('zg-market'), 'CN');
  assert.equal(b.getState().currentMarket, 'US');
  assert.equal(app.createAppStore(sessionB, local).getState().currentMarket, 'US');
  assert.equal(app.createAppStore(memoryStorage(), local).getState().currentMarket, 'CN');
  b.getState().toggleSidebar();
  b.getState().addSearchHistory('AAPL.US');
  assert.equal(local.getItem('zg-default-market'), 'CN');
  assert.equal(a.getState().currentMarket, 'CN');
  assert.throws(() => a.getState().setCurrentMarket('HK'));
});

test('旧历史按标准后缀迁移且去重不丢失；当前历史及持久化按市场独立', () => {
  const local = memoryStorage({
    'zg-app-store': JSON.stringify({ state: { sidebarCollapsed: true, searchHistory: ['AAPL.US', '600519.SH', '00700.HK', '000001.SZ', 'AAPL.US'] }, version: 0 }),
    'zg-search-history-CN': JSON.stringify(['600519.SH', '688001.SH']),
  });
  const store = app.createAppStore(memoryStorage(), local);
  assert.deepEqual(store.getState().searchHistory, ['AAPL.US', '00700.HK']);
  assert.equal(store.getState().sidebarCollapsed, true);
  store.getState().setCurrentMarket('CN');
  assert.deepEqual(store.getState().searchHistory, ['600519.SH', '688001.SH', '000001.SZ']);
  store.getState().addSearchHistory('300750.SZ', 'CN');
  store.getState().addSearchHistory('ADI.US', 'US');
  assert.deepEqual(store.getState().searchHistory, ['300750.SZ', '600519.SH', '688001.SH', '000001.SZ']);
  assert.deepEqual(JSON.parse(local.getItem('zg-search-history-US')), ['ADI.US', 'AAPL.US', '00700.HK']);
  store.getState().clearSearchHistory('CN');
  assert.deepEqual(store.getState().searchHistory, []);
  store.getState().setCurrentMarket('US');
  assert.deepEqual(store.getState().searchHistory, ['ADI.US', 'AAPL.US', '00700.HK']);
  assert.throws(() => store.getState().addSearchHistory('600519.SH', 'US'));
  assert.deepEqual(app.createAppStore(memoryStorage({ 'zg-market': 'CN' }), local).getState().searchHistory, []);
});

test('共享 storage 事件仅同步历史与侧栏，不能改变市场或正在填写的输入', () => {
  const local = memoryStorage();
  const a = app.createAppStore(memoryStorage(), local);
  const b = app.createAppStore(memoryStorage(), local);
  b.getState().setSearchCode('正在填写的交易');
  assert.equal(typeof b.getState().onSharedStorage, 'function');
  a.getState().setCurrentMarket('CN');
  b.getState().onSharedStorage('zg-default-market');
  assert.equal(b.getState().currentMarket, 'US');
  a.getState().addSearchHistory('AAPL.US', 'US');
  b.getState().addSearchHistory('ADI.US', 'US');
  b.getState().onSharedStorage('zg-search-history-US');
  assert.deepEqual(b.getState().searchHistory, ['ADI.US', 'AAPL.US']);
  a.getState().toggleSidebar();
  b.getState().onSharedStorage('zg-sidebar-collapsed');
  assert.equal(b.getState().sidebarCollapsed, true);
  assert.equal(b.getState().searchCode, '正在填写的交易');
  assert.equal(b.getState().currentMarket, 'US');
  assert.equal(local.getItem('zg-default-market'), 'CN');
});

test('存储损坏或被禁用仍有安全的本标签页状态', () => {
  const disabled = { getItem() { throw new Error('禁用'); }, setItem() { throw new Error('禁用'); } };
  const store = app.createAppStore(disabled, disabled);
  assert.equal(store.getState().currentMarket, 'US');
  store.getState().setCurrentMarket('CN');
  store.getState().addSearchHistory('600519.SH');
  assert.deepEqual(store.getState().searchHistory, ['600519.SH']);
  const corrupt = app.createAppStore(memoryStorage(), memoryStorage({ 'zg-app-store': '{bad', 'zg-search-history-US': '{bad' }));
  assert.deepEqual(corrupt.getState().searchHistory, []);
});
