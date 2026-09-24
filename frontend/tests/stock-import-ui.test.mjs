import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { fileURLToPath } from 'node:url';

test('添加弹窗展示服务端结果、市场边界、重试及后台运行语义', async () => {
  const server = await createServer({
    configFile: false, root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true }, appType: 'custom',
  });
  try {
    const { default: StockImportDialog } = await server.ssrLoadModule('/src/components/stock/StockImportDialog.tsx');
    const task = {
      snapshot: { task_id: 'one', status: 'partial_failure', total: 2, processed: 2, success: 1, skipped: 0, failed: 1,
        start_date: '20210101', end_date: '20260923', items: [
          { ts_code: 'WULF.US', name: 'TeraWulf', status: 'completed', rows: 130, indicator_rows: 130,
            first_date: '20260101', last_date: '20260510', message: '行情及指标已就绪' },
          { ts_code: 'BAD.US', name: '', status: 'failed', rows: 0, indicator_rows: 0, message: '未找到有效行情' },
        ] },
      isRunning: false, isSubmitting: false, statusUnreachable: false, isLoading: false,
      submitError: null, start() {}, retry() {}, refresh() {},
    };
    const html = renderToStaticMarkup(createElement(StockImportDialog, { open: true, onClose() {}, task }));
    for (const text of ['普通美股', '50', '五年', '非实时', '关闭不取消', 'WULF.US', '未找到有效行情', '仅重试失败项', '2026-01-01', '130']) {
      assert.ok(html.includes(text), `弹窗缺少：${text}`);
    }
    assert.match(html, /aria-labelledby="stock-import-title"/);
    assert.match(html, /name="codes"/);
    const running = renderToStaticMarkup(createElement(StockImportDialog, {
      open: true, onClose() {}, task: { ...task, isRunning: true, snapshot: { ...task.snapshot, status: 'running' } },
    }));
    assert.ok(!running.includes('仅重试失败项'));
    assert.match(running, /disabled=""/);
  } finally {
    await server.close();
  }
});
