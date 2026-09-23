"""
NASDAQ-100 导入 / 美股港股历史回补脚本测试（us 分支）

覆盖：
- 成分股文件加载与校验（.US 后缀、重复/空 ticker 报错）
- 代码规划：成分股 ∪ 库内现有美股/港股，排除 A 股
- 回补起点计算
- stock_basic 写入口径（market='美股'、不覆盖已有名称、补空板块）
- 导入循环：成功/无数据/失败区分、限流长等待重试、仅有新增时重算指标
"""

import json
from datetime import datetime

import pytest

from tests.conftest import write_klines_to_db, write_stock_basic


def _write_list(tmp_path, constituents):
    path = tmp_path / "nq.json"
    path.write_text(
        json.dumps({"index": "NASDAQ-100", "constituents": constituents}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


# ==================== 名单加载 ====================


def test_load_constituents_appends_us_suffix(tmp_path):
    from scripts.import_nasdaq100 import load_constituents

    path = _write_list(tmp_path, [{"ticker": "nvda", "name": "英伟达", "sector": "AI芯片"}])
    items = load_constituents(path)

    assert items == [{"ts_code": "NVDA.US", "name": "英伟达", "sector": "AI芯片"}]


def test_load_constituents_rejects_duplicate_ticker(tmp_path):
    from scripts.import_nasdaq100 import load_constituents

    path = _write_list(
        tmp_path,
        [{"ticker": "AAPL", "name": "苹果", "sector": "a"}, {"ticker": "AAPL", "name": "苹果2", "sector": "b"}],
    )
    with pytest.raises(ValueError, match="重复"):
        load_constituents(path)


def test_load_constituents_rejects_empty_ticker(tmp_path):
    from scripts.import_nasdaq100 import load_constituents

    path = _write_list(tmp_path, [{"ticker": " ", "name": "x", "sector": "y"}])
    with pytest.raises(ValueError, match="ticker"):
        load_constituents(path)


def test_project_constituent_file_is_valid():
    """项目内固化名单文件本身必须能通过校验且为 101 只"""
    from scripts.import_nasdaq100 import DEFAULT_LIST_PATH, load_constituents

    items = load_constituents(DEFAULT_LIST_PATH)
    assert len(items) == 101
    assert all(i["ts_code"].endswith(".US") for i in items)


# ==================== 代码规划 ====================


def test_plan_codes_unions_constituents_with_existing_us_hk(temp_db, db_conn):
    """成分股与库内现有美股/港股/指数取并集，排除 A 股"""
    from scripts.import_nasdaq100 import plan_codes

    write_klines_to_db(
        db_conn,
        [
            {"ts_code": "02331.HK", "trade_date": "20260918", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0},
            {"ts_code": "SPX.US", "trade_date": "20260918", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0},
            {"ts_code": "AAPL.US", "trade_date": "20260918", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0},
            {"ts_code": "300750.SZ", "trade_date": "20260918", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0},
        ],
    )
    constituents = [{"ts_code": "AAPL.US", "name": "苹果", "sector": "x"}, {"ts_code": "NVDA.US", "name": "英伟达", "sector": "y"}]

    assert plan_codes(constituents, include_existing=True) == ["02331.HK", "AAPL.US", "NVDA.US", "SPX.US"]
    assert plan_codes(constituents, include_existing=False) == ["AAPL.US", "NVDA.US"]


def test_compute_backfill_start():
    from scripts.import_nasdaq100 import compute_backfill_start

    assert compute_backfill_start(datetime(2026, 9, 22, 10, 0), years=5) == "20210922"


def test_latest_windows_use_each_stock_last_date_and_market(temp_db, db_conn):
    from scripts.import_nasdaq100 import plan_latest_windows

    write_klines_to_db(db_conn, [
        {"ts_code": code, "trade_date": date, "open": 1, "high": 1, "low": 1,
         "close": 1, "vol": 1, "amount": 1, "pct_chg": 0}
        for code, date in [("AAPL.US", "20260921"), ("02331.HK", "20260922")]
    ])
    windows = plan_latest_windows(
        ["AAPL.US", "02331.HK", "ARM.US"], "20210923", {"US": "20260922", "HK": "20260922"}
    )

    assert windows == {
        "AAPL.US": ("20260922", "20260922"),
        "02331.HK": ("20260923", "20260922"),
        "ARM.US": ("20210923", "20260922"),
    }


# ==================== stock_basic 写入 ====================


def test_upsert_stock_basic_writes_us_market_and_keeps_existing_name(temp_db, db_conn):
    from scripts.import_nasdaq100 import upsert_stock_basic

    write_stock_basic(db_conn, "AAPL.US", "苹果", industry="", market="美股")
    constituents = [
        {"ts_code": "AAPL.US", "name": "苹果公司", "sector": "消费电子"},
        {"ts_code": "NVDA.US", "name": "英伟达", "sector": "AI芯片"},
    ]

    upsert_stock_basic(db_conn, constituents)

    rows = {
        r[0]: tuple(r)
        for r in db_conn.execute("SELECT ts_code, name, industry, market, area, is_hs FROM stock_basic").fetchall()
    }
    assert rows["NVDA.US"] == ("NVDA.US", "英伟达", "AI芯片", "美股", "美国", "N")
    # 已有记录不改名，但空板块会被补齐
    assert rows["AAPL.US"][1] == "苹果"
    assert rows["AAPL.US"][2] == "消费电子"
    assert rows["AAPL.US"][3] == "美股"


# ==================== 导入循环 ====================


class FakeFetcher:
    """按代码返回写入条数或抛异常（接口与 sync_us_daily 一致）"""

    def __init__(self, outcomes=None):
        self._outcomes = outcomes or {}
        self.calls = []

    def __call__(self, ts_code, start_date, end_date, *, session=None):
        self.calls.append((ts_code, start_date, end_date))
        queue = self._outcomes.get(ts_code, [0])
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _run(codes, fetcher, indicator_calls, sleeps, **kwargs):
    from scripts.import_nasdaq100 import run_import

    return run_import(
        codes,
        start_date="20210922",
        end_date="20260918",
        fetch=fetcher,
        recompute=lambda code: indicator_calls.append(code),
        sleep=lambda s: sleeps.append(s),
        pace=0,
        **kwargs,
    )


def test_run_import_distinguishes_success_no_data_failed():
    fetcher = FakeFetcher({"A.US": [1200], "B.US": [0], "C.US": [Exception("connection reset")]})
    indicator_calls, sleeps = [], []

    summary = _run(["A.US", "B.US", "C.US"], fetcher, indicator_calls, sleeps)

    assert summary["success"] == 1
    assert summary["no_data"] == 1
    assert summary["failed"] == 1
    assert summary["rows"] == 1200
    assert summary["failures"] == [{"ts_code": "C.US", "error": "connection reset"}]
    # 只有真正写入数据的股票才重算指标
    assert indicator_calls == ["A.US"]
    # 网络错误重试 3 次后放弃
    assert [c[0] for c in fetcher.calls].count("C.US") == 3


def test_run_import_rate_limit_waits_long_then_retries():
    fetcher = FakeFetcher({"A.US": [Exception("429 Too Many Requests"), 900]})
    indicator_calls, sleeps = [], []

    summary = _run(["A.US"], fetcher, indicator_calls, sleeps)

    assert summary["success"] == 1
    assert sleeps and sleeps[0] >= 65
    assert len(fetcher.calls) == 2


def test_run_import_passes_backfill_window():
    fetcher = FakeFetcher({"A.US": [10]})

    _run(["A.US"], fetcher, [], [])

    assert fetcher.calls == [("A.US", "20210922", "20260918")]


def test_run_import_indicator_failure_counted_as_failed():
    fetcher = FakeFetcher({"A.US": [10]})

    def boom(code):
        raise RuntimeError("indicator broke")

    from scripts.import_nasdaq100 import run_import

    summary = run_import(
        ["A.US"], start_date="20210922", end_date="20260918", fetch=fetcher, recompute=boom, sleep=lambda s: None, pace=0
    )

    assert summary["failed"] == 1
    assert summary["success"] == 0
    assert "indicator broke" in summary["failures"][0]["error"]
    assert summary["rows"] == 10  # 日线已落库，不能因指标失败漏记。


def test_recompute_detects_swallowed_failure(temp_db, db_conn, monkeypatch):
    from modules.data_sync import DataSyncer
    from scripts.import_nasdaq100 import _make_recompute

    write_klines_to_db(db_conn, [{"ts_code": "ADI.US", "trade_date": "20260918",
        "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0}])
    monkeypatch.setattr(DataSyncer, "sync_indicator_cache", lambda *a, **k: 0)
    with pytest.raises(RuntimeError, match="指标"):
        _make_recompute()("ADI.US")


def test_import_backup_never_overwrites_previous(temp_db, db_conn):
    from scripts.import_nasdaq100 import create_import_backup
    import sqlite3

    first = create_import_backup()
    write_stock_basic(db_conn, "ADI.US", "亚德诺")
    second = create_import_backup()
    assert first != second
    conn = sqlite3.connect(first)
    try:
        assert conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0] == 0
    finally:
        conn.close()


def test_unknown_listing_date_is_not_fabricated(temp_db, db_conn):
    from scripts.import_nasdaq100 import upsert_stock_basic

    upsert_stock_basic(db_conn, [{"ts_code": "ARM.US", "name": "Arm", "sector": "芯片"}])
    assert db_conn.execute("SELECT list_date FROM stock_basic").fetchone()[0] is None


def test_indicators_only_does_not_fetch(temp_db, db_conn, monkeypatch):
    import sys
    from scripts import import_nasdaq100 as module

    write_klines_to_db(db_conn, [{"ts_code": "ADI.US", "trade_date": "20260918",
        "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0}])
    called = []
    monkeypatch.setattr(sys, "argv", ["import", "--indicators-only", "--only", "ADI.US"])
    monkeypatch.setattr(module, "_make_recompute", lambda: lambda code: called.append(code))
    monkeypatch.setattr(module, "_make_fetch", lambda *a: pytest.fail("不应联网"))
    assert module.main() == 0
    assert called == ["ADI.US"]


def test_latest_import_skips_covered_dates_and_distinguishes_empty_source(temp_db):
    from scripts.import_nasdaq100 import run_import

    fetcher = FakeFetcher({"A.US": [1], "B.US": [0]})
    indicator_calls = []
    summary = run_import(
        ["A.US", "B.US", "C.US"], "20210923", "20260922", fetcher,
        indicator_calls.append, sleep=lambda _: None, pace=0,
        windows={"A.US": ("20260922", "20260922"),
                 "B.US": ("20260922", "20260922"),
                 "C.US": ("20260923", "20260922")},
    )
    assert fetcher.calls == [("A.US", "20260922", "20260922"), ("B.US", "20260922", "20260922")]
    assert indicator_calls == ["A.US", "B.US", "C.US"]
    assert (summary["success"], summary["no_data"], summary["no_change"], summary["rows"]) == (1, 1, 1, 1)


def test_latest_repairs_missing_indicators_without_fetch(temp_db, db_conn):
    from scripts.import_nasdaq100 import run_import

    write_klines_to_db(db_conn, [{"ts_code": "A.US", "trade_date": "20260922",
        "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0}])
    fetcher, called = FakeFetcher(), []
    summary = run_import(
        ["A.US"], "20210923", "20260922", fetcher, called.append, pace=0,
        windows={"A.US": ("20260923", "20260922")},
    )
    assert fetcher.calls == []
    assert called == ["A.US"]
    assert summary["no_change"] == 1


@pytest.mark.parametrize("rows,expected_exit", [(1, 0), (0, 2), (ConnectionError("timeout"), 1)])
def test_latest_cli_incremental_window_exit_and_session_cleanup(temp_db, db_conn, monkeypatch, rows, expected_exit):
    from unittest.mock import Mock
    from modules import yahoo_sync
    from scripts import import_nasdaq100 as module

    write_klines_to_db(db_conn, [{"ts_code": "ADI.US", "trade_date": "20260921",
        "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0}])
    session, fetcher = Mock(), FakeFetcher({"ADI.US": [rows]})
    monkeypatch.setattr(yahoo_sync, "_default_session", lambda: session)
    monkeypatch.setattr(module, "_make_fetch", lambda s, **kwargs: fetcher)
    monkeypatch.setattr(module, "_beijing_now", lambda: datetime(2026, 9, 23, 15))
    monkeypatch.setattr(module, "NETWORK_BACKOFF", 0)
    monkeypatch.setattr(module, "_make_recompute", lambda: pytest.fail("日常更新不应重算历史指标"))
    result = module.main(["--latest", "--only", "ADI.US", "--pace", "0"])
    assert result == expected_exit
    assert all(call == ("ADI.US", "20260922", "20260922") for call in fetcher.calls)
    session.close.assert_called_once()


def test_latest_dry_run_has_no_fetch_backup_or_write(temp_db, db_conn, monkeypatch, capsys):
    from scripts import import_nasdaq100 as module
    from modules import yahoo_sync

    monkeypatch.setattr(module, "create_import_backup", lambda: pytest.fail("不应备份"))
    monkeypatch.setattr(yahoo_sync, "_default_session", lambda: pytest.fail("不应联网"))
    monkeypatch.setattr(module, "upsert_stock_basic", lambda *a: pytest.fail("不应写库"))
    assert module.main(["--latest", "--dry-run", "--only", "ADI.US"]) == 0
    assert "ADI.US" in capsys.readouterr().out
    assert db_conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0] == 0


def test_latest_stops_before_fetch_if_backup_fails(temp_db, monkeypatch):
    from scripts import import_nasdaq100 as module
    from modules import yahoo_sync

    def fail_backup():
        raise OSError("backup unavailable")

    monkeypatch.setattr(module, "create_import_backup", fail_backup)
    monkeypatch.setattr(yahoo_sync, "_default_session", lambda: pytest.fail("备份失败后不应联网"))
    monkeypatch.setattr(module, "upsert_stock_basic", lambda *a: pytest.fail("备份失败后不应写库"))
    with pytest.raises(OSError, match="backup unavailable"):
        module.main(["--latest", "--only", "ADI.US"])


def test_latest_rejects_indicators_only(temp_db):
    from scripts import import_nasdaq100 as module

    with pytest.raises(SystemExit) as exc:
        module.main(["--latest", "--indicators-only"])
    assert exc.value.code == 2
