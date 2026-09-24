"""新股票收录的隔离回归；所有行情响应均为测试样本，不访问网络或业务库。"""

import pytest


def test_normalize_single_and_bulk():
    from api.services.stock_import_service import normalize_codes

    assert normalize_codes("wulf") == ["WULF.US"]
    assert normalize_codes(" wulf, WULF.US\niren\tBRK.B，brk-b.us ") == ["WULF.US", "IREN.US", "BRK-B.US"]


@pytest.mark.parametrize(
    "text", ["", "  ", "00700.HK", "^GSPC", "600519.SH", "SPX", "AAPL/../../x", "AAPL;DROP", "AAPL.US.US"]
)
def test_invalid_input_rejected(text):
    from api.services.stock_import_service import normalize_codes

    with pytest.raises(ValueError):
        normalize_codes(text)


def test_batch_limit_applies_after_deduplication():
    from api.services.stock_import_service import normalize_codes

    assert normalize_codes("WULF " * 60) == ["WULF.US"]
    with pytest.raises(ValueError):
        normalize_codes(" ".join(f"S{i}" for i in range(51)))


def chart_payload(symbol="WULF", count=130):
    from datetime import datetime, timedelta, timezone

    dates = [datetime(2026, 1, 1, 16, tzinfo=timezone.utc) + timedelta(days=i) for i in range(count)]
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "instrumentType": "EQUITY",
                        "currency": "USD",
                        "exchangeTimezoneName": "America/New_York",
                        "shortName": "测试公司",
                        "gmtoffset": -14400,
                    },
                    "timestamp": [int(day.timestamp()) for day in dates],
                    "indicators": {
                        "quote": [
                            {
                                "open": [10.0] * count,
                                "close": [10.5] * count,
                                "high": [11.0] * count,
                                "low": [9.0] * count,
                                "volume": [100] * count,
                            }
                        ]
                    },
                }
            ]
        }
    }


def source_session(payload):
    from tests.test_yahoo_sync import FakeSession, FakeResponse

    return FakeSession([FakeResponse(payload)])


def test_import_atomically_registers_valid_stock(temp_db, db_conn):
    from modules.yahoo_sync import import_us_stock

    result = import_us_stock("WULF.US", "20210101", "20260923", session=source_session(chart_payload()))
    assert result["rows"] == 130
    assert result["first_date"] == "20260101"
    assert tuple(db_conn.execute("SELECT name,market,industry,list_date FROM stock_basic").fetchone()) == (
        "测试公司",
        "美股",
        "",
        None,
    )
    assert db_conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0] == 130


@pytest.mark.parametrize("fault", ["symbol", "market", "type", "empty", "nan", "ohlc"])
def test_invalid_source_never_registers_stock(temp_db, db_conn, fault):
    from modules.yahoo_sync import import_us_stock

    payload = chart_payload()
    node = payload["chart"]["result"][0]
    if fault == "symbol":
        node["meta"]["symbol"] = "OTHER"
    elif fault == "market":
        node["meta"]["currency"] = "HKD"
    elif fault == "type":
        node["meta"]["instrumentType"] = "ETF"
    elif fault == "empty":
        node["timestamp"] = []
    else:
        node["indicators"]["quote"][0]["close"][0] = float("nan") if fault == "nan" else 20
    with pytest.raises(ValueError):
        import_us_stock("WULF.US", "20210101", "20260923", session=source_session(payload))
    assert db_conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0] == 0


def test_import_sql_failure_rolls_back_basic_and_prices(temp_db, db_conn):
    import sqlite3
    from modules.yahoo_sync import import_us_stock

    db_conn.execute("CREATE TRIGGER fail_bar BEFORE INSERT ON daily_kline BEGIN SELECT RAISE(ABORT,'test'); END")
    db_conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        import_us_stock("WULF.US", "20210101", "20260923", session=source_session(chart_payload()))
    assert db_conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0] == 0


def test_market_lock_is_exclusive_and_released(temp_db):
    from modules.market_sync_lock import MarketSyncLease, SyncBusyError

    with MarketSyncLease.acquire("first"):
        assert MarketSyncLease.is_running("first")
        assert not MarketSyncLease.is_running("other")
        with pytest.raises(SyncBusyError):
            MarketSyncLease.acquire("second")
    assert not MarketSyncLease.is_running("first")
    with MarketSyncLease.acquire("second"):
        assert MarketSyncLease.is_running("second")


def test_market_lock_blocks_other_process_and_process_exit_releases(temp_db):
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path
    from modules.database import get_db_path
    from modules.market_sync_lock import MarketSyncLease, SyncBusyError

    env = os.environ.copy()
    env["DB_PATH"] = str(get_db_path())
    script = (
        "from modules.market_sync_lock import MarketSyncLease;import time;"
        "lease=MarketSyncLease.acquire('other-process');time.sleep(30)"
    )
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", script],
        cwd=Path(__file__).parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(f"锁进程提前退出：{stdout}{stderr}")
            if MarketSyncLease.is_running("other-process"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("其他进程未在预期时间内取得系统锁")
        with pytest.raises(SyncBusyError):
            MarketSyncLease.acquire("local")
    finally:
        process.kill()
        process.wait(timeout=10)
    assert not MarketSyncLease.is_running("other-process")


@pytest.fixture
def import_service(temp_db, tmp_path):
    from datetime import datetime
    from api.services.stock_import_service import StockImportService
    from modules.yahoo_sync import import_us_stock

    def fetch(code, start, end):
        return import_us_stock(code, start, end, session=source_session(chart_payload(code.removesuffix(".US"))))

    service = StockImportService(
        fetch=fetch, sleep=lambda _: None, now=lambda: datetime(2026, 9, 24, 10), backup_dir=tmp_path
    )
    yield service
    service.shutdown()


def finished(service, text):
    first = service.submit(text)
    assert service.wait_for_completion(30)
    return service.get_status(first["task_id"])


def test_background_import_and_existing_skip(import_service, db_conn):
    from api.services.sync_service import collect_us_hk_codes
    from modules.data_freshness import get_data_status

    before_version = get_data_status()["version"]
    result = finished(import_service, "WULF, IREN")
    assert result["status"] == "completed"
    assert result["success"] == 2
    assert result["start_date"] == "20210924"
    assert result["end_date"] == "20260923"
    assert result["items"][0]["indicator_rows"] == 130
    assert collect_us_hk_codes() == ["IREN.US", "WULF.US"]
    assert get_data_status()["ready"]
    assert get_data_status()["version"] != before_version
    before = [tuple(row) for row in db_conn.execute("SELECT * FROM daily_kline")]
    result = finished(import_service, "WULF")
    assert result["skipped"] == 1
    assert [tuple(row) for row in db_conn.execute("SELECT * FROM daily_kline")] == before
    assert db_conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0


def test_retry_only_failed_indicators_without_redownload(import_service):
    from modules.indicators.cache_builder import write_indicator_rows

    calls = []
    original = import_service.fetch

    def fetch(*args):
        calls.append(args[0])
        return original(*args)

    def compute(code):
        if code == "WULF.US":
            raise RuntimeError("测试指标故障")
        return write_indicator_rows(code)

    import_service.fetch = fetch
    import_service.compute = compute
    first = finished(import_service, "WULF IREN")
    assert first["status"] == "partial_failure"
    assert first["failed"] == 1
    import_service.compute = write_indicator_rows
    retry = import_service.retry(first["task_id"])
    assert import_service.wait_for_completion(30)
    result = import_service.get_status(retry["task_id"])
    assert result["status"] == "completed"
    assert result["total"] == 1
    assert result["items"][0]["ts_code"] == "WULF.US"
    assert result["start_date"] == first["start_date"]
    assert calls == ["WULF.US", "IREN.US"]


def test_duplicate_and_conflicting_tasks(import_service):
    import threading
    from modules.market_sync_lock import SyncBusyError

    gate, started = threading.Event(), threading.Event()
    fetch = import_service.fetch

    def slow(*args):
        started.set()
        assert gate.wait(10)
        return fetch(*args)

    import_service.fetch = slow
    first = import_service.submit("WULF")
    try:
        assert started.wait(5)
        assert import_service.submit("wulf.us")["task_id"] == first["task_id"]
        with pytest.raises(SyncBusyError):
            import_service.submit("IREN")
        from api.services.sync_service import USStockSyncService

        daily = USStockSyncService()
        try:
            with pytest.raises(SyncBusyError):
                daily.submit()
        finally:
            daily._executor.shutdown(wait=True)
    finally:
        gate.set()
        assert import_service.wait_for_completion(30)


def test_interrupted_snapshot_can_be_retried(import_service, db_conn):
    import json

    snapshot = {
        "task_id": "crashed",
        "status": "running",
        "phase": "download",
        "start_date": "20210101",
        "end_date": "20260923",
        "items": [{"ts_code": "WULF.US", "status": "downloading", "owned": True}],
        "total": 1,
        "processed": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
    }
    db_conn.execute(
        "INSERT INTO sync_log(data_type,ts_code,status,message) VALUES ('stock_import','crashed','running',?)",
        (json.dumps(snapshot),),
    )
    db_conn.commit()
    before = db_conn.total_changes
    assert import_service.get_status("crashed")["status"] == "interrupted"
    assert db_conn.total_changes == before
    retry = import_service.retry("crashed")
    assert import_service.wait_for_completion(30)
    assert import_service.get_status(retry["task_id"])["success"] == 1


def test_failed_item_reports_safe_source_reason(import_service):
    def reject(code, start, end):
        from modules.yahoo_sync import import_us_stock

        payload = chart_payload(code.removesuffix(".US"))
        payload["chart"]["result"][0]["meta"]["instrumentType"] = "ETF"
        return import_us_stock(code, start, end, session=source_session(payload))

    import_service.fetch = reject
    result = finished(import_service, "WULF")
    assert result["status"] == "failed"
    assert "标的信息不匹配" in result["items"][0]["message"]
    assert "代理" not in result["items"][0]["message"]


def test_import_backup_failure_leaves_market_data_untouched(import_service, db_conn, monkeypatch):
    from api.services import stock_import_service as module

    def fail(*args, **kwargs):
        raise OSError("磁盘故障")

    monkeypatch.setattr(module, "backup_database", fail)
    result = finished(import_service, "WULF")
    assert result["status"] == "failed"
    assert db_conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0] == 0
