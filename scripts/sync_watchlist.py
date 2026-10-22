#!/usr/bin/env python3
"""
[薄壳] 同步自选股清单中"在 daily_kline 表里完全缺失"的股票
v2.10.0 重构：业务逻辑迁至 modules.data_sync.DataSyncer.sync_missing
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.database import market_context
from modules.data_sync import sync_workspace_stocks
from scripts._common import load_watchlist


def main():
    p = argparse.ArgumentParser(description="同步缺失的自选股 K 线（薄壳）")
    p.add_argument("--days", type=int, default=730, help="同步天数")
    p.add_argument("--market", choices=["US", "CN"], default="US", help="工作空间市场（默认US）")
    args = p.parse_args()
    with market_context(args.market):
        return _run(args)


def _run(args):
    ts_codes = load_watchlist()
    if not ts_codes:
        print("自选股清单为空")
        return

    results = sync_workspace_stocks(ts_codes, days=args.days, mode="missing")
    synced = sum(1 for v in results.values() if v > 0)
    print(f"sync_missing 完成: 总 {len(ts_codes)} 只, 补齐 {len(results)} 只, 成功 {synced} 只")


if __name__ == "__main__":
    main()
