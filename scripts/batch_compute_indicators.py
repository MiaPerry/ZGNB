#!/usr/bin/env python3
"""
[薄壳] 批量计算自选股清单的指标缓存（已有 K 线，只算指标）
v2.10.0 重构：业务逻辑迁至 modules.data_sync.DataSyncer.sync_all_indicators
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.database import market_context
from modules.data_sync import sync_workspace_stocks
from scripts._common import load_watchlist


def main():
    p = argparse.ArgumentParser(description="批量计算自选股的指标缓存（薄壳）")
    p.add_argument("--market", choices=["US", "CN"], default="US", help="工作空间市场（默认US）")
    args = p.parse_args()
    with market_context(args.market):
        return _run(args)


def _run(args):
    ts_codes = load_watchlist()
    if not ts_codes:
        print("自选股清单为空")
        return

    results = sync_workspace_stocks(ts_codes, mode="indicators")
    synced = sum(1 for v in results.values() if v > 0)
    print(f"sync_all_indicators 完成: 总 {len(ts_codes)} 只, 成功 {synced} 只")


if __name__ == "__main__":
    main()
