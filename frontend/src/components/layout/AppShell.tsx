import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { useAppStore } from '../../stores/appStore';
import { stockDetailDecision } from '../../lib/stockListQuery';
import { useDataStatus } from '../../hooks/useDataStatus';
import { apiErrorMessage } from '../../api/client';
import ApiErrorState from '../ui/ApiErrorState';
import { workspaceContentGate } from '../../lib/syncLinkage';
import Sidebar from './Sidebar';
import Header from './Header';

export default function AppShell() {
  const market = useAppStore((s) => s.currentMarket);
  const initialized = useAppStore((s) => s.marketInitialized);
  const location = useLocation();
  const decision = stockDetailDecision(location.pathname, market, initialized);
  const dataStatus = useDataStatus();
  const contentGate = workspaceContentGate(market, dataStatus);
  return (
    <div className="flex h-screen overflow-hidden bg-transparent">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <Header />
        <main className="flex-1 overflow-auto p-4">
          {decision === 'wait' ? <p role="status">正在初始化工作空间…</p>
            : decision === 'reject' ? <Navigate to="/stocks" replace state={{ marketNotice: `代码与 ${market} 工作空间不符，已返回当前市场股票库。` }} />
            : contentGate === 'pending' ? <p role="status">正在核实 CN 数据工作空间…</p>
            : contentGate === 'error' ? <ApiErrorState message={apiErrorMessage(dataStatus.error)} onRetry={() => void dataStatus.refetch()} />
            : <>
              {dataStatus.isError && <p role="alert" className="mb-3 text-sm text-accent-gold">数据状态刷新失败：{apiErrorMessage(dataStatus.error)}。当前保留上次已核实的页面与输入，数据状态尚未更新。</p>}
              {typeof location.state?.marketNotice === 'string' && <p role="status" className="mb-3 text-sm text-accent-gold">{location.state.marketNotice}</p>}
              <Outlet key={market} />
            </>}
        </main>
      </div>
    </div>
  );
}
