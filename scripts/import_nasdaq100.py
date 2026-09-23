#!/usr/bin/env python3
"""
NASDAQ-100 成分股导入 + 美股/港股历史回补脚本（us 分支）

- 从 data/nasdaq100.json 读取固化的成分股名单，写入 stock_basic（market='美股'）
- 对成分股 ∪ 库内现有美股/港股/指数，经 Yahoo Chart API（Clash 代理）回补最近 N 年日线
- 有新增数据的股票按全部历史条数重算 indicator_cache
- 写入前通过 SQLite backup API 备份；限流/网络错误分级重试

用法：
    python scripts/import_nasdaq100.py --dry-run             # 只打印计划，不联网不写库
    python scripts/import_nasdaq100.py                       # 5 年回补，含库内现有股票
    python scripts/import_nasdaq100.py --years 3 --no-existing
    python scripts/import_nasdaq100.py --only NVDA.US,ARM.US # 只处理指定代码
    python scripts/import_nasdaq100.py --skip-indicators     # 只拉 K 线不算指标
    python scripts/import_nasdaq100.py --indicators-only     # 离线补算全部历史指标
    python scripts/import_nasdaq100.py --latest              # 日常增量更新：纳指100 + 原有美股/港股
    python scripts/import_nasdaq100.py --latest --dry-run    # 查看逐只增量区间，不联网不写库

增量模式退出码：0=更新完成或已覆盖查询上界，1=有失败，2=数据源未返回部分新行情。
--latest 增量更新日线并补算缺失日期指标，保留已有历史指标，不更新盘中未收盘日线。
停牌、休市或源延迟均可能返回空数据。
"""

import argparse
import json
import logging
import sys
import time
from contextlib import ExitStack, closing
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.services.sync_service import (  # noqa: E402
    _beijing_now,
    _is_us_hk,
    _market_of,
    backup_database,
    classify_error,
    compute_market_end_date,
)
from modules.database import get_connection, get_db_path  # noqa: E402
from scripts._common import PROJECT_ROOT  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_LIST_PATH = PROJECT_ROOT / "data" / "nasdaq100.json"

# 与 sync_service 保持一致的重试参数
RATE_LIMIT_WAIT = 65.0
NETWORK_BACKOFF = 5.0
MAX_ATTEMPTS = 3
# 相邻请求间隔，避免触发 Yahoo 限流
DEFAULT_PACE = 1.5


# ==================== 名单与计划 ====================


def load_constituents(path) -> list[dict]:
    """读取成分股文件，返回 [{ts_code, name, sector}]；ticker 为空或重复时抛 ValueError"""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)

    items, seen = [], set()
    for raw in payload["constituents"]:
        ticker = str(raw.get("ticker", "")).strip().upper()
        if not ticker:
            raise ValueError(f"成分股 ticker 为空: {raw}")
        if ticker in seen:
            raise ValueError(f"成分股 ticker 重复: {ticker}")
        seen.add(ticker)
        items.append({"ts_code": f"{ticker}.US", "name": raw.get("name", ticker), "sector": raw.get("sector", "")})
    return items


def plan_codes(constituents: list[dict], include_existing: bool) -> list[str]:
    """待处理代码：成分股，可选并入库内现有美股/港股/指数（排除 A 股）"""
    codes = {c["ts_code"] for c in constituents}
    if include_existing:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT ts_code FROM stock_basic UNION SELECT ts_code FROM daily_kline"
            ).fetchall()
        codes.update(r[0] for r in rows if r[0] and _is_us_hk(r[0]))
    return sorted(codes)


def compute_backfill_start(now: datetime, years: int) -> str:
    """回补起点 = 当前日期往前 N 年（同月同日）"""
    try:
        start = now.replace(year=now.year - years)
    except ValueError:  # 2 月 29 日
        start = now.replace(year=now.year - years, day=28)
    return start.strftime("%Y%m%d")


def plan_latest_windows(codes, fallback_start, end_by_market):
    """逐只从已有日线的下一天补齐；无历史的股票仍按指定年数首拉。"""
    with get_connection() as conn:
        latest = dict(conn.execute("SELECT ts_code, MAX(trade_date) FROM daily_kline GROUP BY ts_code"))
    windows = {}
    for code in codes:
        last = latest.get(code)
        start = (datetime.strptime(last, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d") if last else fallback_start
        windows[code] = (start, end_by_market[_market_of(code)])
    return windows


def upsert_stock_basic(conn, constituents: list[dict]) -> None:
    """写入成分股基本信息：新股票插入；已有股票不改名，仅补齐空板块"""
    cursor = conn.cursor()
    for c in constituents:
        cursor.execute(
            """
            INSERT OR IGNORE INTO stock_basic
            (ts_code, name, area, industry, market, list_date, is_hs)
            VALUES (?, ?, '美国', ?, '美股', NULL, 'N')
            """,
            (c["ts_code"], c["name"], c["sector"]),
        )
        cursor.execute(
            "UPDATE stock_basic SET industry = ? WHERE ts_code = ? AND (industry IS NULL OR industry = '')",
            (c["sector"], c["ts_code"]),
        )
    conn.commit()


# ==================== 导入循环 ====================


def _fetch_with_retry(fetch, ts_code, start_date, end_date, sleep):
    """带分级重试的日线拉取；返回 (rows, error)"""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fetch(ts_code, start_date, end_date), None
        except Exception as e:
            kind = classify_error(e)
            logger.warning("拉取失败 %s（第 %d 次，%s）: %s", ts_code, attempt, kind, e)
            if attempt >= MAX_ATTEMPTS:
                return None, str(e)
            sleep(RATE_LIMIT_WAIT if kind == "rate_limit" else NETWORK_BACKOFF * attempt)
    return None, "超过最大重试次数"


def run_import(codes, start_date, end_date, fetch, recompute, sleep=time.sleep, pace=DEFAULT_PACE, on_progress=None, windows=None):
    """
    逐只下载日线，可选执行指标计算

    Args:
        fetch: (ts_code, start_date, end_date) -> 写入条数，失败抛异常
        recompute: (ts_code) -> None，指标重算，失败抛异常；传 None 时不处理指标
        windows: 可选的 {代码: (增量起点, 市场上界)}，已覆盖时跳过下载
    Returns:
        {"success", "no_data", "no_change", "failed", "rows", "failures": [{ts_code, error}]}
    """
    summary = {"success": 0, "no_data": 0, "no_change": 0, "failed": 0, "rows": 0, "indicator_rows": 0, "failures": []}
    for i, code in enumerate(codes, 1):
        start, end = windows[code] if windows is not None else (start_date, end_date)
        covered = start > end
        rows, error = (0, None) if covered else _fetch_with_retry(fetch, code, start, end, sleep)
        if error is None:
            summary["rows"] += rows
            try:
                if recompute is not None and (rows or windows is not None):
                    summary["indicator_rows"] += recompute(code) or 0
            except Exception as e:
                error = f"指标计算失败: {e}"

        if error is not None:
            summary["failed"] += 1
            summary["failures"].append({"ts_code": code, "error": error})
            state = f"失败: {error}"
        elif rows:
            summary["success"] += 1
            state = f"{rows} 条"
        elif covered:
            summary["no_change"] += 1
            state = "已覆盖查询上界，无需下载"
        else:
            summary["no_data"] += 1
            state = "数据源未返回区间内新行情" if windows is not None else "无数据"

        if on_progress:
            on_progress(i, len(codes), code, state)
        if pace and not covered and i < len(codes):
            sleep(pace)
    return summary


# ==================== 生产装配 ====================


def create_import_backup():
    """每次运行独立备份，重试不会覆盖首次导入前的恢复点。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return backup_database(get_db_path().parent / "backups" / f"pre-nasdaq100-{stamp}.db")


def _make_fetch(session, *, overwrite=True):
    from modules.yahoo_sync import sync_us_daily

    return lambda ts_code, start_date, end_date: sync_us_daily(ts_code, start_date, end_date, session=session, incremental=not overwrite)


def _make_recompute():
    from modules.data_sync import DataSyncer

    syncer = DataSyncer()

    def recompute(ts_code):
        with get_connection() as conn:
            days = conn.execute("SELECT COUNT(*) FROM daily_kline WHERE ts_code = ?", (ts_code,)).fetchone()[0]
        written = syncer.sync_indicator_cache(ts_code, days=days)
        if written != days or days == 0:
            raise RuntimeError(f"指标未完整写入 {ts_code}: {written}/{days}")
        return written

    return recompute


def main(argv=None):
    p = argparse.ArgumentParser(description="NASDAQ-100 + 原有美股/港股：历史回补或 --latest 增量更新")
    p.add_argument("--list", default=str(DEFAULT_LIST_PATH), help="成分股 JSON 文件")
    p.add_argument("--years", type=int, default=5, help="回补年数（默认 5）")
    p.add_argument("--no-existing", action="store_true", help="不回补库内现有美股/港股，只处理成分股")
    p.add_argument("--only", default="", help="只处理指定代码，逗号分隔（如 NVDA.US,ARM.US）")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--skip-indicators", action="store_true", help="只拉 K 线，不重算指标")
    mode.add_argument("--indicators-only", action="store_true", help="离线重算指标，不下载日线")
    p.add_argument("--pace", type=float, default=DEFAULT_PACE, help="相邻请求间隔秒数")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不联网不写库")
    p.add_argument("--latest", action="store_true", help="增量更新日线并补齐缺失指标，不覆盖已有历史指标")
    args = p.parse_args(argv)
    if not 1 <= args.years <= 50 or args.pace < 0:
        p.error("years 必须为 1~50，pace 不能为负数")
    if args.latest and args.indicators_only:
        p.error("--latest 与 --indicators-only 不能同时使用")
    if not get_db_path().is_file():
        p.error(f"数据库不存在，请检查 DB_PATH：{get_db_path()}")

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    constituents = load_constituents(args.list)
    codes = plan_codes(constituents, include_existing=not args.no_existing)
    if args.only:
        wanted = {c.strip().upper() for c in args.only.split(",") if c.strip()}
        unknown = wanted - set(codes)
        if unknown:
            p.error(f"不在计划中的代码: {sorted(unknown)}")
        codes = [c for c in codes if c in wanted]

    now = _beijing_now()
    start_date = compute_backfill_start(now, args.years)
    end_by_market = {m: compute_market_end_date(now, m) for m in ("US", "HK")}
    windows = plan_latest_windows(codes, start_date, end_by_market) if args.latest else None

    print("=" * 60)
    label = "最新行情增量更新" if args.latest else "NASDAQ-100 导入 / 历史回补"
    print(f"{label} — 库: {get_db_path()}")
    print(f"成分股 {len(constituents)} 只，待处理 {len(codes)} 只，目标日 US:{end_by_market['US']} HK:{end_by_market['HK']}")
    if not args.latest:
        print(f"历史回补起点 {start_date}")
    print("=" * 60)
    if args.latest:
        pending = sum(start <= end for start, end in windows.values())
        print(f"需查询 {pending} 只，已覆盖查询上界 {len(codes) - pending} 只；增量日线并补齐缺失指标")
    if args.skip_indicators:
        print("注意：本次仅更新日线，指标未更新，分析可能尚未就绪。")
    if args.dry_run:
        for c in codes:
            if windows is None:
                print("  ", c)
            else:
                start, end = windows[c]
                print(f"  {c:<10} {start} ~ {end}" if start <= end else f"  {c:<10} 已覆盖查询上界")
        print("\n[dry-run] 未联网、未写库")
        return 0

    backup = create_import_backup()
    print(f"已备份到 {backup}")

    indicator_worker = None

    def recompute(code):
        nonlocal indicator_worker
        if args.skip_indicators:
            return
        if args.latest:
            from modules.data_sync import sync_indicator_cache_incremental
            return sync_indicator_cache_incremental(code)
        print(f"    {code} 正在重算全历史指标……", flush=True)
        if indicator_worker is None:
            indicator_worker = _make_recompute()
        return indicator_worker(code)

    def progress(i, total, code, state):
        print(f"  [{i}/{total}] {code:<10} {state}", flush=True)

    t0 = time.time()
    with ExitStack() as resources:
        from modules.data_freshness import data_update_batch
        resources.enter_context(data_update_batch())
        if args.indicators_only:
            def fetch(code, _start, _end):
                with get_connection() as conn:
                    return conn.execute("SELECT COUNT(*) FROM daily_kline WHERE ts_code = ?", (code,)).fetchone()[0]
        else:
            selected = [c for c in constituents if c["ts_code"] in codes]
            with get_connection() as conn:
                upsert_stock_basic(conn, selected)
            print(f"stock_basic 已写入/补齐 {len(selected)} 只成分股")
            from modules.yahoo_sync import _default_session

            if windows is None or any(start <= end for start, end in windows.values()):
                session = resources.enter_context(closing(_default_session()))
                fetch = _make_fetch(session, overwrite=not args.latest)
            else:
                def fetch(*_):
                    return 0
        # 各市场查询上界不同：按代码所属市场逐只传入
        summary = run_import(
            codes,
            start_date,
            end_by_market["US"],
            fetch=lambda code, s, _e: fetch(code, s, end_by_market.get(_market_of(code), end_by_market["US"])),
            recompute=None if args.skip_indicators else recompute,
            pace=0 if args.indicators_only else args.pace,
            on_progress=progress,
            windows=windows,
        )

    print("\n" + "=" * 60)
    print(
        f"完成，用时 {time.time() - t0:.0f}s：成功 {summary['success']} · 无数据 {summary['no_data']} "
        f"· 失败 {summary['failed']} · 日线 {summary['rows']} 条 · 指标 {summary['indicator_rows']} 条"
    )
    if args.latest:
        print(f"无需下载 {summary['no_change']} 只")
        if summary["no_data"]:
            print("注意：部分查询未返回新行情，可能为休市、停牌或源延迟；未视为已更新，请稍后重跑。")
    for f in summary["failures"]:
        print(f"  ✗ {f['ts_code']}: {f['error']}")
    print("=" * 60)
    if args.latest:
        return 1 if summary["failed"] else (2 if summary["no_data"] else 0)
    return 1 if summary["failed"] or summary["no_data"] else 0


if __name__ == "__main__":
    sys.exit(main())
