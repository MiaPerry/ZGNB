import { useEffect, useRef } from 'react';
import Button from '../ui/Button';
import type { useStockImport } from '../../hooks/useStockImport';
import { canRetryImport, IMPORT_LABELS } from '../../lib/stockImportTask';

const dateLabel = (date: string | null) => date ? `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}` : '--';

type Props = { open: boolean; onClose: () => void; task: ReturnType<typeof useStockImport> };

export default function StockImportDialog({ open, onClose, task }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (open && !element.open) element.showModal();
    if (!open && element.open) element.close();
  }, [open]);
  const { snapshot } = task;
  const busy = task.isRunning || task.isSubmitting;

  return (
    <dialog ref={dialog} onClose={onClose} aria-labelledby="stock-import-title" aria-describedby="stock-import-help"
      className="m-auto max-h-[90dvh] w-[calc(100%-2rem)] max-w-2xl overflow-y-auto rounded-xl border border-border bg-bg-card p-5 text-text-primary shadow-2xl backdrop:bg-black/70">
      <div className="flex items-center justify-between gap-3">
        <h2 id="stock-import-title" className="text-lg font-semibold">添加股票 · 历史初始化</h2>
        <Button type="button" variant="ghost" onClick={onClose} aria-label="关闭添加股票">关闭</Button>
      </div>
      <div id="stock-import-help" className="mt-3 flex items-start gap-2 text-xs leading-5 text-accent-gold/90">
        <svg aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
          <circle cx="12" cy="12" r="9" /><path d="M12 7v6m0 3v1" />
        </svg>
        <div className="space-y-1">
          <p>仅支持普通美股（不含 ETF、指数、港股）。单只或批量最多 50 个去重代码，用逗号、空格或换行分隔。</p>
          <p>默认从五年前或库内普通美股最早日期中较早者开始下载。实际历史以数据源为准，新上市股票可能不足五年；价格为日线收盘价，非实时行情。</p>
          <p>已存在股票跳过，不覆盖历史。行情与全部指标齐备后才计为成功；不会自动加入自选、点评或回测。</p>
          <p>任务在后台执行，关闭不取消；重新打开可继续查看进度。添加期间不能同时更新行情。</p>
        </div>
      </div>
      <form className="mt-4 space-y-3" onSubmit={(event) => {
        event.preventDefault();
        if (!busy) task.start(String(new FormData(event.currentTarget).get('codes') ?? ''));
      }}>
        <label htmlFor="stock-import-codes" className="block text-sm">美股代码</label>
        <textarea id="stock-import-codes" name="codes" required maxLength={5000} rows={3} disabled={busy}
          placeholder={'WULF, IREN\nBRK.B'} autoCapitalize="characters" spellCheck={false}
          className="w-full resize-y rounded-lg border border-border bg-bg-primary p-3 font-mono text-sm outline-none focus:border-accent-gold disabled:opacity-50" />
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={busy || task.isLoading || task.statusUnreachable}>
            {task.isSubmitting ? '提交中…' : task.isRunning ? '后台处理中…' : '开始添加'}
          </Button>
          {canRetryImport(snapshot) && <Button type="button" variant="secondary" disabled={busy || task.statusUnreachable} onClick={task.retry}>仅重试失败项</Button>}
          <Button type="button" variant="ghost" onClick={task.refresh}>刷新进度</Button>
        </div>
      </form>
      {task.submitError && <p role="alert" className="mt-3 text-sm text-accent-red">{task.submitError}</p>}
      {task.statusUnreachable && <p role="alert" className="mt-3 text-sm text-accent-gold">进度连接异常，后台任务可能仍在运行，请刷新进度后再操作。</p>}
      {task.isLoading && <p role="status" className="mt-4 text-sm text-text-muted">正在恢复任务进度…</p>}
      {snapshot?.task_id && (
        <section className="mt-5 border-t border-border pt-4" aria-label="最近添加任务">
          <div role="status" className="text-sm">
            {IMPORT_LABELS[snapshot.status]} · 已处理 {snapshot.processed}/{snapshot.total}
            <span className="ml-2 text-xs text-text-secondary">成功 {snapshot.success} · 跳过 {snapshot.skipped} · 失败/中断 {snapshot.failed}</span>
          </div>
          <progress aria-label="股票添加进度" max={snapshot.total || 1} value={snapshot.processed} className="mt-2 h-2 w-full accent-accent-gold" />
          <p className="mt-2 text-xs text-text-muted">请求窗口：{dateLabel(snapshot.start_date)} 至 {dateLabel(snapshot.end_date)} · {IMPORT_LABELS[snapshot.phase] ?? snapshot.phase}</p>
          <ul className="mt-3 divide-y divide-border/50">
            {snapshot.items.map((item) => (
              <li key={item.ts_code} className="space-y-1 py-3 text-xs">
                <div className="flex flex-wrap justify-between gap-2">
                  <span className="break-all font-semibold">{item.ts_code} <span className="font-normal text-text-secondary">{item.name}</span></span>
                  <span className={['failed', 'interrupted'].includes(item.status) ? 'text-accent-red' : 'text-accent-gold'}>{IMPORT_LABELS[item.status]}</span>
                </div>
                <p className="break-words text-text-secondary">{item.message}</p>
                {item.rows > 0 && <p className="text-text-muted">实际覆盖：{dateLabel(item.first_date)} 至 {dateLabel(item.last_date)} · 日线 {item.rows} 条 · 已确认指标 {item.indicator_rows} 条</p>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </dialog>
  );
}
