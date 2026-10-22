import assert from 'node:assert/strict';
import { test } from 'node:test';
import { QueryClient } from '@tanstack/react-query';
import { readFileSync } from 'node:fs';
import * as tasks from '../src/lib/syncLinkage.ts';

const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };

test('迟到写回只清空同市场仍未修改的表单，不能覆盖新输入', () => {
  assert.equal(typeof tasks.submittedDraftMatches, 'function');
  assert.equal(tasks.submittedDraftMatches('CN', 'US', ['买入'], ['买入']), false);
  assert.equal(tasks.submittedDraftMatches('US', 'US', ['后来输入'], ['买入']), false);
  assert.equal(tasks.submittedDraftMatches('US', 'US', ['AAPL.US', '新标签'], ['AAPL.US', '旧标签']), false);
  assert.equal(tasks.submittedDraftMatches('US', 'US', ['买入'], ['买入']), true);
  for (const page of ['Trades', 'Watchlist']) {
    const source = readFileSync(new URL(`../src/pages/${page}.tsx`, import.meta.url), 'utf8');
    assert.ok(source.includes('submittedDraftMatches'));
    assert.match(source, /set(?:InputText|Draft)\(\(current\)/, '回调必须读取功能式更新中的最新表单');
  }
});

test('旧筛选即使手动重试也只能写原市场键，未提交不能请求', async () => {
  assert.equal(typeof tasks.screenQueryOptions, 'function');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  const waiting = deferred();
  const calls = [];
  const run = (request, signal) => { calls.push({ request, signal }); return waiting.promise; };
  const submitted = { market: 'US', strategy: 'B1', limit: 20 };
  const options = tasks.screenQueryOptions('CN', submitted, run);
  assert.deepEqual(options.queryKey, ['market', 'US', 'screen', 'B1', 20]);
  assert.equal(options.enabled, false);
  const pending = client.fetchQuery(options);
  waiting.resolve({ market: 'US' });
  await pending;
  assert.deepEqual(client.getQueryData(options.queryKey), { market: 'US' });
  assert.equal(client.getQueryData(['market', 'CN', 'screen', 'B1', 20]), undefined);
  assert.equal(calls[0].request.market, 'US');
  assert.ok(calls[0].signal instanceof AbortSignal);
  await assert.rejects(client.fetchQuery(tasks.screenQueryOptions('CN', null, run)), /尚未提交/);
  client.clear();
});

test('延迟读响应和重试保持发起市场，取消只作用原市场读取', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
  let active = 'US';
  const captured = active;
  const calls = [];
  const waiting = deferred();
  const options = { queryKey: tasks.marketKey(captured, 'test-read'), retry: 1, retryDelay: 0,
    queryFn: async ({ signal }) => { calls.push({ market: captured, signal }); if (calls.length === 1) { await waiting.promise; throw new Error('重试'); } return captured; } };
  const pending = client.fetchQuery(options);
  active = 'CN';
  client.setQueryData(tasks.marketKey(active, 'test-read'), 'CN');
  waiting.resolve();
  assert.equal(await pending, 'US');
  assert.deepEqual(calls.map((c) => c.market), ['US', 'US']);
  assert.equal(client.getQueryData(tasks.marketKey(active, 'test-read')), 'CN');
  let signal;
  const blocked = client.fetchQuery({ queryKey: tasks.marketKey('US', 'blocked'), queryFn: (ctx) => { signal = ctx.signal; return new Promise(() => {}); } });
  const rejected = assert.rejects(blocked);
  await client.cancelQueries({ queryKey: tasks.marketKey('US') });
  await rejected;
  assert.equal(signal.aborted, true);
  assert.equal(client.getQueryData(tasks.marketKey('CN', 'test-read')), 'CN');
  client.clear();
});
