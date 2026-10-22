#!/usr/bin/env python3
"""安全初始化CN数据库；默认只做显式命令，不覆盖已有目标。"""

import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from modules.cn_initializer import CNInitializer, inspect_cn_initialization  # noqa: E402
from modules.data_sync import sync_cn_daily  # noqa: E402
from modules.database import get_db_path  # noqa: E402
from modules.tushare_client import TushareClient  # noqa: E402


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _five_year_start(cutoff):
    date = datetime.strptime(cutoff, "%Y%m%d")
    try:
        return date.replace(year=date.year - 5).strftime("%Y%m%d")
    except ValueError:
        return date.replace(year=date.year - 5, day=28).strftime("%Y%m%d")


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="旧A股源库；首次初始化必填")
    parser.add_argument("--target", type=Path, help="CN目标库，默认使用CN_DB_PATH")
    parser.add_argument("--snapshot", type=Path, help="已核验的沪深300名单快照JSON")
    parser.add_argument("--preflight", type=Path, help="核心接口及完整历史预检JSON")
    parser.add_argument("--cutoff", help="固定行情截止日YYYYMMDD")
    parser.add_argument("--resume", metavar="TASK_ID", help="恢复固定任务，不改变名单或截止日")
    parser.add_argument("--dry-run", action="store_true", help="只读输出空间、源表和代码规模")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    target = (args.target or get_db_path("CN")).resolve()
    manifest = None
    if args.resume:
        manifest_path = target.parent / f".cn-init-{args.resume}" / "manifest.json"
        manifest = _load(manifest_path)
        source = Path(manifest["source"])
        snapshot = manifest["constituent_snapshot"]
        cutoff = manifest["cutoff_date"]
        preflight = _load(args.preflight) if args.preflight else manifest["core_preflight"]
    else:
        if not args.source or not args.snapshot or not args.cutoff:
            _parser().error("首次初始化必须提供 --source、--snapshot 和 --cutoff")
        source, snapshot, cutoff = args.source.resolve(), _load(args.snapshot), args.cutoff
        preflight = _load(args.preflight) if args.preflight else None
        manifest_path = None
    if args.dry_run:
        print(json.dumps(inspect_cn_initialization(source, target, snapshot, cutoff), ensure_ascii=False))
        return 0
    if preflight is None:
        _parser().error("写入staging前必须提供 --preflight 核心预检报告")
    if manifest is None:
        initializer = CNInitializer(source, target, snapshot, cutoff, core_preflight=preflight)
    else:
        initializer = CNInitializer.resume(manifest_path, source=source, target=target,
                                           constituent_snapshot=snapshot, cutoff_date=cutoff,
                                           core_preflight=preflight)
    state = initializer.start()
    client = TushareClient()
    five_year = _five_year_start(cutoff)
    fixed_source = Path(state["source_snapshot"])

    def initialize_stock(code, end_date, progress):
        with closing(sqlite3.connect(fixed_source)) as conn:
            row = conn.execute("SELECT MIN(trade_date) FROM daily_kline WHERE ts_code=?", (code,)).fetchone()
        start = min(five_year, row[0]) if row and row[0] else five_year
        sync_cn_daily(client, code, start, end_date, history_repair=True, progress_callback=progress)

    initializer.stock_initializer = initialize_stock
    print(json.dumps(initializer.run(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"CN初始化失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
