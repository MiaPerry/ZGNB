import assert from 'node:assert/strict';
import { test } from 'node:test';
import { QueryClient, QueryObserver } from '@tanstack/react-query';
import { IMPORT_TASK_KEY, mergeImportSnapshot, canRetryImport, importQueryOptions } from '../src/lib/stockImportTask.ts';

const job = (status, revision = 1, sequence = 1) => ({ task_id: `job-${sequence}`, status, revision, sequence, failed: 0, items: [] });

test('迟到的提交快照不能覆盖已完成轮询', () => {
  const done = job('completed', 6);
  assert.equal(mergeImportSnapshot(done, job('running')), done);
  assert.equal(mergeImportSnapshot(undefined, done), done);
});

test('新任务不会被旧任务响应覆盖，重试仍能进入运行态', () => {
  const retry = job('running', 1, 2);
  assert.equal(mergeImportSnapshot(retry, job('failed', 9)), retry);
  assert.equal(mergeImportSnapshot(job('failed', 9), retry), retry);
});

test('派生中断状态不被相同修订号的迟到运行态覆盖', () => {
  const interrupted = job('interrupted', 3);
  assert.equal(mergeImportSnapshot(interrupted, job('running', 3)), interrupted);
});

test('轮询采用两秒运行间隔，错误不覆盖已有任务', async () => {
  const client = new QueryClient();
  const options = importQueryOptions(async () => { throw new Error('offline'); });
  assert.equal(options.refetchInterval({ state: { data: job('running') } }), 2000);
  assert.equal(options.refetchInterval({ state: { data: job('completed') } }), 30000);
  client.setQueryData(IMPORT_TASK_KEY, job('running', 3));
  await assert.rejects(client.fetchQuery({ ...options, retry: false }));
  assert.equal(client.getQueryData(IMPORT_TASK_KEY).status, 'running');
  client.clear();
});

test('只允许重试失败或中断的终态任务', () => {
  assert.equal(canRetryImport({ ...job('partial_failure'), failed: 1 }), true);
  assert.equal(canRetryImport({ ...job('interrupted'), failed: 1 }), true);
  assert.equal(canRetryImport({ ...job('running'), failed: 1 }), false);
  assert.equal(canRetryImport(job('completed')), false);
});

test('查询缓存持有终态，重新打开观察器仍可读取结果', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  const completed = job('completed', 6);
  const options = { queryKey: IMPORT_TASK_KEY, queryFn: async () => completed, structuralSharing: mergeImportSnapshot };
  const observer = new QueryObserver(client, options);
  const off = observer.subscribe(() => {});
  await observer.refetch();
  client.setQueryData(IMPORT_TASK_KEY, (old) => mergeImportSnapshot(old, job('running')));
  off();
  const reopened = new QueryObserver(client, options);
  assert.equal(reopened.getCurrentResult().data.status, 'completed');
  client.clear();
});
