#!/usr/bin/env python3
"""
每日定时同步脚本：复刻前端"同步最新数据"按钮的行为（美股/港股 Yahoo 日线 + 指标增量）

由 Windows 任务计划程序每日 08:00 调用；进程独立运行，不依赖后端是否已启动。
- 与网页/后端共用 data/stock_data.market-sync.lock 跨进程互斥，撞车时安全跳过
- 同步前备份沿用 USStockSyncService 内部逻辑，本脚本只做旧备份滚动清理
- Yahoo 拉取需要 Clash 代理（默认 127.0.0.1:7890，可用 YAHOO_PROXY 覆盖）
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # .env 中 DB_PATH=data/stock_data.db 为相对路径，须先切到仓库根目录

LOG_DIR = ROOT / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "daily_us_hk_sync.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("daily_us_hk_sync")

KEEP_BACKUPS = 5  # 每次同步前会整库备份（约 400MB+），只保留最近 N 份防止占满磁盘
MAX_WAIT_SECONDS = 2 * 3600
POLL_INTERVAL = 10


def proxy_available() -> bool:
    """检查 Yahoo 依赖的本地代理端口是否可达"""
    proxy = os.environ.get("YAHOO_PROXY", "http://127.0.0.1:7890").strip()
    parsed = urlparse(proxy)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 7890
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        logger.error("代理 %s:%d 不可达", host, port)
        return False


def prune_backups(keep: int = KEEP_BACKUPS) -> list[str]:
    """滚动清理 us-hk-pre-sync-*.db 备份，只保留最近 keep 份"""
    backup_dir = ROOT / "data" / "backups"
    if not backup_dir.is_dir():
        return []
    backups = sorted(
        backup_dir.glob("us-hk-pre-sync-*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for old in backups[keep:]:
        old.unlink()
        removed.append(old.name)
    return removed


def main() -> int:
    from api.services.sync_service import USStockSyncService
    from modules.market_sync_lock import SyncBusyError

    logger.info("=== 每日同步启动 %s ===", datetime.now().isoformat(timespec="seconds"))

    if not proxy_available():
        logger.error("Clash 代理未就绪，跳过本次同步（Yahoo 数据源需要代理）")
        return 2

    service = USStockSyncService()
    try:
        snap = service.submit()
    except SyncBusyError as e:
        logger.warning("另一同步任务正在进行，跳过本次：%s", e)
        return 3

    logger.info("任务已提交 task_id=%s，等待完成…", snap.task_id)
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        snap = service.get_status()
        if snap.status != "running":
            break
        time.sleep(POLL_INTERVAL)
    else:
        logger.error("等待超时（%ds），任务线程可能仍在后台运行", MAX_WAIT_SECONDS)
        return 4

    removed = prune_backups()
    if removed:
        logger.info("滚动清理旧备份 %d 个: %s", len(removed), removed)

    logger.info(
        "同步结束 status=%s 处理=%d/%d 成功=%d 无新增=%d 无返回=%d 失败=%d "
        "新增K线=%d 补算指标=%d 数据日期=%s 消息=%s",
        snap.status, snap.processed, snap.total, snap.success, snap.no_change,
        snap.no_data, snap.failed, snap.new_rows, snap.indicator_rows,
        snap.data_date, snap.message,
    )
    if snap.failures:
        logger.info("失败明细: %s", json.dumps(snap.failures, ensure_ascii=False))
    return 0 if snap.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
