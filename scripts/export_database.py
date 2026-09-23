#!/usr/bin/env python3
"""本地导出 WAL 安全的一致性 SQLite 快照；不会上传或覆盖已有文件。"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from api.services.sync_service import backup_database  # noqa: E402


def export_database(source: Path, output: Path | None = None) -> dict:
    """源库只读；完整校验成功后才以排他创建方式发布独立快照。"""
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError("源数据库不存在，禁止创建空库后导出")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = (
        Path(output)
        if output is not None
        else (source.parent / "backups" / "exports" / f"{source.stem}-{stamp}-{uuid.uuid4().hex[:8]}.db")
    )
    output = output.absolute()
    if output.resolve() == source:
        raise ValueError("快照不能输出到源数据库")
    if output.exists() or output.is_symlink():
        raise FileExistsError("输出文件已存在，禁止覆盖旧快照")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".export-", dir=output.parent) as staging:
        snapshot = Path(staging) / "snapshot.db"
        backup_database(snapshot, source_path=source)
        with closing(sqlite3.connect(snapshot)) as conn:
            # 只在快照上切换日志模式，使交付物无需额外 WAL/SHM 文件。
            conn.execute("PRAGMA journal_mode=DELETE")
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("导出快照完整性校验失败")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_log'").fetchone():
                from modules.data_freshness import rotate_data_epoch
                rotate_data_epoch(conn)
                conn.execute("UPDATE sync_log SET status='finished' WHERE data_type='data_batch'")
                conn.commit()
            tables = {}
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ):
                quoted = '"' + name.replace('"', '""') + '"'
                tables[name] = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            latest = None
            if "daily_kline" in tables:
                latest = conn.execute("SELECT MAX(trade_date) FROM daily_kline").fetchone()[0]
        digest = hashlib.sha256()
        with snapshot.open("rb") as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(chunk)
            src.seek(0)
            # xb 防止检查之后其他进程创建同名文件；失败只清理本次创建的文件。
            with output.open("xb") as dest:
                try:
                    shutil.copyfileobj(src, dest)
                    dest.flush()
                    os.fsync(dest.fileno())
                except BaseException:
                    dest.close()
                    output.unlink()
                    raise
    return {"path": str(output), "sha256": digest.hexdigest(), "tables": tables, "latest_trade_date": latest}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="默认使用当前环境 DB_PATH（相对路径基于项目根目录）")
    parser.add_argument("--output", type=Path, help="可选的新快照路径；默认放到源库旁 backups/exports 下")
    args = parser.parse_args(argv)
    source = args.source
    if source is None:
        source = Path(os.environ.get("DB_PATH", "data/stock_data.db"))
        if not source.is_absolute():
            source = PROJECT_ROOT / source
    try:
        print(json.dumps(export_database(source, args.output), ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
