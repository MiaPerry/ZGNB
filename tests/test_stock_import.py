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


def test_normalize_cn_requires_standard_stock_codes(temp_db):
    from api.services.stock_import_service import normalize_codes
    from modules.database import init_database, market_context

    init_database("CN")
    with market_context("CN"):
        assert normalize_codes("600519.sh, 000001.SZ 600519.SH") == ["600519.SH", "000001.SZ"]
        for text in ("600519", "000300.SH", "830001.BJ", "AAPL.US"):
            with pytest.raises(ValueError):
                normalize_codes(text)


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


@pytest.mark.parametrize("module_name,getter_name", [
    ("api.services.sync_service", "get_a_share_sync_service"),
    ("api.services.stock_import_service", "get_stock_import_service"),
])
def test_service_registry_is_keyed_by_market_and_path(temp_db, tmp_path, monkeypatch, module_name, getter_name):
    import importlib
    from modules.database import market_context

    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "_services", {}, raising=False)
    monkeypatch.setattr(module, "_service", None, raising=False)
    getter = getattr(module, getter_name)
    instances = []
    try:
        us = getter()
        instances.append(us)
        assert getter() is us
        with market_context("CN"):
            cn = getter()
            instances.append(cn)
            assert getter() is cn
            assert cn is not us
            assert cn.binding.market == "CN"
        monkeypatch.setenv("CN_DB_PATH", str(tmp_path / "another-cn.db"))
        with market_context("CN"):
            other = getter()
            instances.append(other)
            assert other is not cn
            assert other.binding.db_path == tmp_path / "another-cn.db"
        assert getter() is us
    finally:
        for instance in set(instances):
            instance._executor.shutdown(wait=True)


def test_cn_import_registers_verified_metadata_then_uses_atomic_sync(temp_db, tmp_path):
    from datetime import datetime
    import pandas as pd
    from api.services.stock_import_service import StockImportService
    from modules import data_freshness, data_sync
    from modules.database import get_connection, init_database, market_context
    from tests.test_data_sync_extensions import _cn_daily_source

    class Client:
        def get_stock_basic(self, ts_code=None, list_status="L", raise_on_error=False):
            if list_status != "L":
                return pd.DataFrame()
            return pd.DataFrame([{"ts_code": ts_code, "name": "贵州茅台", "area": "贵州",
                                  "industry": "白酒", "market": "主板", "list_date": "20010827",
                                  "is_hs": "N", "list_status": "L"}])

    client, calls = Client(), []

    def cn_sync(client_arg, code, start_date, end_date, *, lease):
        with get_connection() as conn:
            asset = dict(conn.execute("SELECT * FROM cn_asset_metadata WHERE ts_code=?", (code,)).fetchone())
        assert asset["asset_type"] == "stock" and asset["metadata_status"] == "complete"
        daily, factors = _cn_daily_source(code)
        candidate = data_sync.build_cn_daily_candidate(code, daily, factors)
        written = data_sync.publish_cn_daily_candidate(
            candidate, data_freshness.get_data_version(code), lease=lease)
        calls.append((client_arg, code, start_date, end_date))
        return {"new_rows": len(candidate["bars"]), "indicator_rows": written}

    init_database("CN")
    service = StockImportService(
        market="CN", cn_client=client, cn_sync=cn_sync, compute=lambda code: 0,
        sleep=lambda _: None, now=lambda: datetime(2026, 9, 24, 10), backup_dir=tmp_path,
    )
    try:
        task = service.submit("600519.SH")
        assert service.wait_for_completion(30)
        result = service.get_status(task["task_id"])
        assert result["status"] == "completed" and result["success"] == 1
        assert calls == [(client, "600519.SH", "20210924", "20260923")]
        with market_context("CN"), get_connection() as conn:
            assert tuple(conn.execute("SELECT name,market FROM stock_basic").fetchone()) == ("贵州茅台", "主板")
            assert conn.execute("SELECT COUNT(*) FROM cn_daily_publication").fetchone()[0] == 1
    finally:
        service.shutdown()


def test_cn_import_failure_removes_new_unpublished_registration(temp_db, tmp_path):
    from datetime import datetime
    import pandas as pd
    from api.services.stock_import_service import StockImportService
    from modules.database import get_connection, init_database, market_context

    class Client:
        def get_stock_basic(self, ts_code=None, list_status="L", raise_on_error=False):
            if list_status != "L":
                return pd.DataFrame()
            return pd.DataFrame([{"ts_code": ts_code, "name": "测试股", "market": "主板",
                                  "list_status": "L"}])

    def fail(*args, **kwargs):
        raise ConnectionError("测试网络故障")

    init_database("CN")
    service = StockImportService(
        market="CN", cn_client=Client(), cn_sync=fail, sleep=lambda _: None,
        now=lambda: datetime(2026, 9, 24, 10), backup_dir=tmp_path,
    )
    try:
        task = service.submit("600519.SH")
        assert service.wait_for_completion(30)
        assert service.get_status(task["task_id"])["status"] == "failed"
        with market_context("CN"), get_connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM cn_asset_metadata").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0] == 0
    finally:
        service.shutdown()


@pytest.mark.parametrize("bad_identity", [{"workspace_market": "CN"}, {"db_path": "other.db"}])
def test_import_rejects_persisted_task_from_another_identity(import_service, bad_identity):
    from modules.database import get_db_path

    job = {"task_id": "foreign", "workspace_market": "US", "db_path": str(get_db_path()),
           "status": "failed", "items": [{"ts_code": "WULF.US", "status": "failed"}]}
    job.update(bad_identity)
    import_service._save(job, create=True)
    with pytest.raises(ValueError, match="归属"):
        import_service.get_status("foreign")
    with pytest.raises(ValueError, match="归属"):
        import_service.retry("foreign")


def test_import_retry_validates_persisted_codes(import_service, monkeypatch):
    from modules.database import get_connection

    job = {"task_id": "bad-code", "status": "failed", "start_date": "20210101", "end_date": "20260918",
           "items": [{"ts_code": "600519.SH", "status": "failed"}]}
    import_service._save(job, create=True)
    monkeypatch.setattr(import_service, "_run", lambda job, lease: lease.__exit__())
    with pytest.raises(ValueError, match="代码"):
        import_service.retry("bad-code")
    with get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sync_log WHERE data_type='stock_import'").fetchone()[0] == 1


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


def test_import_service_pins_original_market_for_submit_worker_and_status(import_service, db_conn):
    import threading
    from modules.database import get_connection, get_market, init_database, market_context
    from modules.market_sync_lock import MarketSyncLease

    init_database("CN")
    started, release = threading.Event(), threading.Event()
    fetch = import_service.fetch
    observed = []

    def slow(*args):
        observed.append(get_market())
        started.set()
        assert release.wait(5)
        return fetch(*args)

    import_service.fetch = slow
    try:
        with market_context("CN"), MarketSyncLease.acquire("cn-running"):
            first = import_service.submit("WULF")
            assert started.wait(5)
            status = import_service.get_status(first["task_id"])
            assert status["status"] == "running"
            assert status["workspace_market"] == "US"
            assert import_service.submit("WULF")["task_id"] == first["task_id"]
            release.set()
            assert import_service.wait_for_completion(10)
            assert import_service.get_status(first["task_id"])["status"] == "completed"
            with get_connection() as conn:
                assert conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0] == 0
                assert conn.execute("SELECT COUNT(*) FROM sync_log WHERE data_type='stock_import'").fetchone()[0] == 0
        assert observed == ["US"]
        assert db_conn.execute("SELECT ts_code FROM stock_basic").fetchone()[0] == "WULF.US"
    finally:
        release.set()


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
