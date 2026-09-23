import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import Header from './Header';

export default function AppShell() {
  return (
    <div className="flex h-screen overflow-hidden bg-transparent">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <Header />
        <div role="note" aria-label="行情配色约定" className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-border/40 px-4 py-1.5 text-xs text-text-muted">
          <span>当前配色：</span>
          <span className="text-up">红涨</span>
          <span className="text-down">绿跌</span>
          <span>（沿用 A 股习惯）</span>
        </div>
        <main className="flex-1 overflow-auto p-4">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
