import assert from 'node:assert/strict';
import { test } from 'node:test';
import { QueryClient, QueryObserver } from '@tanstack/react-query';
import {
  DATA_STATUS_KEY,
  isTerminalSnapshot,
  refreshAfterSync,
  shouldHandleTask,
} from '../src/lib/syncLinkage.ts';

test('只有带任务ID的终态快照才触发联动', () => {
  assert.equal(isTerminalSnapshot(null), false);
  assert.equal(isTerminalSnapshot(undefined), false);
  assert.equal(isTerminalSnapshot({ task_id: null, status: 'completed' }), false);
  assert.equal(isTerminalSnapshot({ task_id: 't1', status: 'idle' }), false);
  assert.equal(isTerminalSnapshot({ task_id: 't1', status: 'running' }), false);
  assert.equal(isTerminalSnapshot({ task_id: 't1', status: 'completed' }), true);
  assert.equal(isTerminalSnapshot({ task_id: 't1', status: 'partial_failure' }), true);
  assert.equal(isTerminalSnapshot({ task_id: 't1', status: 'failed' }), true);
});

test('同一任务只联动一次，新任务可以再次联动', () => {
  const snapshot = { task_id: 't1', status: 'completed' };
  assert.equal(shouldHandleTask(null, snapshot), true);
  assert.equal(shouldHandleTask('t1', snapshot), false);
  assert.equal(shouldHandleTask('t1', { task_id: 't2', status: 'completed' }), true);
  assert.equal(shouldHandleTask(null, { task_id: 't1', status: 'running' }), false);
});

function observe(queryClient, queryKey, counter, { enabled = true, initialData } = {}) {
  if (initialData !== undefined) queryClient.setQueryData(queryKey, initialData);
  const observer = new QueryObserver(queryClient, {
    queryKey,
    queryFn: async () => {
      counter.count += 1;
      return counter.count;
    },
    enabled,
    retry: false,
    staleTime: Infinity,
  });
  return observer.subscribe(() => {});
}

test('终态联动：常规查询刷新、已运行扫描重跑、未运行扫描与AI点评零调用', async () => {
  const queryClient = new QueryClient();
  const stock = { count: 0 };
  const screen = { count: 0 };
  const scan = { count: 0 };
  const commentary = { count: 0 };
  const status = { count: 0 };

  const unsubs = [
    observe(queryClient, ['stock', 'AAPL.US', 120], stock),
    // 已运行过的选股：页面在运行后启用查询并持有结果
    observe(queryClient, ['screen', 'B1', 20], screen, { initialData: { count: 1 } }),
    // 未运行过的扫描：页面保持禁用，联动不得触发首次执行
    observe(queryClient, ['dashboard-scan'], scan, { enabled: false }),
    observe(queryClient, ['commentary', 'AAPL.US', 120], commentary),
    observe(queryClient, DATA_STATUS_KEY, status),
  ];

  await queryClient.refetchQueries({ type: 'active' });
  const base = { stock: stock.count, screen: screen.count, commentary: commentary.count, status: status.count };
  assert.equal(scan.count, 0);

  refreshAfterSync(queryClient);
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.equal(stock.count, base.stock + 1, '行情查询应主动刷新一次');
  assert.equal(screen.count, base.screen + 1, '已运行的选股应自动重跑一次');
  assert.equal(status.count, base.status + 1, '数据状态应刷新一次');
  assert.equal(scan.count, 0, '未运行的扫描不得自动启动');
  assert.equal(commentary.count, base.commentary, 'AI 点评不得产生额外调用');

  for (const unsub of unsubs) unsub();
});

test('终态联动不刷新未在联动清单内的其他查询', async () => {
  const queryClient = new QueryClient();
  const other = { count: 0 };
  const unsub = observe(queryClient, ['backtest-result'], other);
  await queryClient.refetchQueries({ type: 'active' });
  const base = other.count;
  refreshAfterSync(queryClient);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(other.count, base);
  unsub();
});
