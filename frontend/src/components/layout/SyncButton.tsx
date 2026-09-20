import { useState } from 'react';
import { useBatchSync } from '../../hooks/useBatchSync';

/** 后端阶段码 → 展示文案（展示层格式化，不涉及数据口径） */
const PHASE_LABELS: Record<string, string> = {
  idle: '空闲',
  backup: '备份数据',
  sync: '同步日线',
  indicators: '重算指标',
  done: '完成',
};

const STATUS_LABELS: Record<string, string> = {
  idle: '空闲',
  running: '同步中',
  completed: '已完成',
  partial_failure: '部分失败',
  failed: '失败',
};

export default function SyncButton() {
  const { snapshot, isRunning, start, isStarting, startError, statusUnreachable } =
    useBatchSync();
  const [panelOpen, setPanelOpen] = useState(false);

  const running = isRunning || isStarting;
  const status = snapshot?.status ?? 'idle';
  const hasResult = status !== 'idle' && status !== 'running';

  const buttonLabel = running
    ? `同步中 ${snapshot?.processed ?? 0}/${snapshot?.total ?? 0}`
    : '同步最新数据';

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => (hasResult || startError ? setPanelOpen((v) => !v) : start())}
        onContextMenu={(e) => {
          e.preventDefault();
          setPanelOpen((v) => !v);
        }}
        disabled={running}
        aria-disabled={running}
        aria-live="polite"
        title={
          running
            ? `正在${PHASE_LABELS[snapshot?.phase ?? 'idle']}${
                snapshot?.current_code ? `：${snapshot.current_code}` : ''
              }`
            : '同步当前库中全部 A 股的日线与技术指标'
        }
        className={`flex items-center gap-1.5 rounded px-2.5 py-1.5 text-xs transition-colors ${
          running
            ? 'cursor-not-allowed bg-bg-hover text-text-muted'
            : status === 'failed' || status === 'partial_failure'
              ? 'bg-accent-red/15 text-accent-red hover:bg-accent-red/25'
              : 'bg-accent-gold/15 text-accent-gold hover:bg-accent-gold/25'
        }`}
      >
        {running && (
          <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent-gold" />
        )}
        <span className="hidden sm:inline">{buttonLabel}</span>
        <span className="sm:hidden">{running ? `${snapshot?.processed ?? 0}/${snapshot?.total ?? 0}` : '同步'}</span>
      </button>

      {panelOpen && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setPanelOpen(false)} />
          <div className="absolute right-0 top-full z-50 mt-1 w-80 rounded-md border border-border bg-bg-secondary shadow-lg">
            <div className="flex items-center justify-between border-b border-border/40 px-3 py-2">
              <span className="text-xs font-medium text-text-primary">
                数据同步 · {STATUS_LABELS[status]}
              </span>
              {snapshot?.task_id && (
                <button
                  type="button"
                  onClick={() => {
                    setPanelOpen(false);
                    if (!running) start();
                  }}
                  disabled={running}
                  className="text-xs text-accent-gold hover:underline disabled:cursor-not-allowed disabled:text-text-muted"
                >
                  {running ? '进行中…' : '再次同步'}
                </button>
              )}
            </div>

            <div className="space-y-1.5 px-3 py-2 text-xs text-text-secondary">
              {statusUnreachable && (
                <p className="text-accent-red">连接异常：无法获取任务状态，后台任务可能仍在运行。</p>
              )}
              {startError && <p className="text-accent-red">提交失败：{startError}</p>}

              {snapshot && snapshot.task_id && (
                <>
                  <div className="flex justify-between">
                    <span className="text-text-muted">阶段</span>
                    <span>
                      {PHASE_LABELS[snapshot.phase]}
                      {snapshot.phase !== 'done' && snapshot.current_code
                        ? ` · ${snapshot.current_code}`
                        : ''}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-text-muted">进度</span>
                    <span>
                      {snapshot.processed}/{snapshot.total}（新增 {snapshot.new_rows} 条）
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-text-muted">结果</span>
                    <span>
                      成功 {snapshot.success} · 无新增 {snapshot.no_change} · 失败 {snapshot.failed}
                    </span>
                  </div>
                  {snapshot.data_date && (
                    <div className="flex justify-between">
                      <span className="text-text-muted">数据日期</span>
                      <span className="font-mono">{snapshot.data_date}</span>
                    </div>
                  )}
                  {snapshot.message && (
                    <p className="text-text-muted">{snapshot.message}</p>
                  )}

                  {snapshot.failures.length > 0 && (
                    <div className="mt-1 max-h-32 overflow-y-auto rounded border border-border/40 bg-bg-primary">
                      {snapshot.failures.map((f) => (
                        <div key={f.ts_code} className="border-b border-border/20 px-2 py-1 last:border-0">
                          <span className="font-mono text-text-primary">{f.ts_code}</span>
                          <span className="ml-2 break-all text-accent-red">{f.error}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}

              {(!snapshot || !snapshot.task_id) && !startError && (
                <p className="text-text-muted">尚未运行同步任务，点击按钮开始。</p>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
