"""
美股/港股一键批量同步服务测试（us 分支）

覆盖：
- 股票名单并集与市场过滤（.US/.HK）
- 各市场查询上界计算（美东/香港时区、收盘前后、周末回退）
- 增量起点、无新增、失败区分、限流重试、防重入、备份保护
- 无需 TUSHARE_TOKEN 即可同步（走 Yahoo）
"""

import threading
from datetime import datetime, timedelta

import pytest

from tests.conftest import write_klines_to_db, write_stock_basic


@pytest.mark.parametrize("period", ["分钟", "小时"])
def test_cn_free_frequency_limits_are_classified_as_rate_limit(period):
    from api.services.sync_service import classify_error
    from modules.tushare_client import TushareDataError, TushareClient
    from unittest.mock import MagicMock

    message = f"抱歉，您访问接口频率超限(1次/{period})"
    assert classify_error(RuntimeError(message)) == "rate_limit"
    client = TushareClient.__new__(TushareClient)
    client._pro = MagicMock()
    client._pro.adj_factor.side_effect = RuntimeError(message)
    client._rate_limit = lambda: None
    with pytest.raises(TushareDataError) as error:
        client.get_adj_factor("600519.SH", "20211021", "20261021")
    assert error.value.kind == "rate_limit"
    assert "限频" in str(error.value)


# ==================== 名单收集与市场过滤 ====================


def test_collect_codes_unions_stock_basic_and_kline(temp_db, db_conn):
    """stock_basic 与 daily_kline 取并集：有行情但缺基本信息的股票不能漏"""
    from api.services.sync_service import collect_us_hk_codes

    write_stock_basic(db_conn, "AAPL.US", "苹果")
    write_klines_to_db(
        db_conn,
        [
            {"ts_code": "02331.HK", "trade_date": "20260918", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0},
        ],
    )

    codes = collect_us_hk_codes()
    assert codes == ["02331.HK", "AAPL.US"]


def test_collect_codes_excludes_a_share(temp_db, db_conn):
    """排除 A 股（.SH/.SZ/.BJ）等非美股/港股代码"""
    from api.services.sync_service import collect_us_hk_codes

    write_klines_to_db(
        db_conn,
        [
            {"ts_code": "AAPL.US", "trade_date": "20260918", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0},
            {"ts_code": "02331.HK", "trade_date": "20260918", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0},
            {"ts_code": "300750.SZ", "trade_date": "20260918", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0},
        ],
    )

    assert collect_us_hk_codes() == ["02331.HK", "AAPL.US"]


def test_collect_codes_empty_db(temp_db):
    """空库返回空名单，不抛异常"""
    from api.services.sync_service import collect_us_hk_codes

    assert collect_us_hk_codes() == []


def test_collect_workspace_codes_uses_cn_registered_assets(temp_db):
    from api.services.sync_service import collect_workspace_codes
    from modules.data_sync import register_cn_assets
    from modules.database import get_connection, init_database, market_context

    init_database("CN")
    with market_context("CN"), get_connection() as conn:
        register_cn_assets(conn, [
            {"ts_code": "600519.SH", "name": "贵州茅台", "market": "主板", "asset_type": "stock",
             "currency": "CNY", "sources": ["manual"]},
            {"ts_code": "000300.SH", "name": "沪深300", "market": "指数", "asset_type": "index",
             "currency": "CNY", "sources": ["reference"]},
        ])
        conn.execute("INSERT INTO stock_basic(ts_code,name,market) VALUES ('000001.SZ','未登记','主板')")
    with market_context("CN"):
        assert collect_workspace_codes() == ["000300.SH", "600519.SH"]


# ==================== 各市场查询上界 ====================


def test_us_end_date_rolls_back_weekend():
    """美东时区：周日北京时间 → 最近交易日回退到上周五"""
    from api.services.sync_service import compute_market_end_date

    # 2026-09-20 周日 10:00 北京 → 美东 09-19 22:00 周六 → 回退到周五 0918
    assert compute_market_end_date(datetime(2026, 9, 20, 10, 0), "US") == "20260918"


def test_hk_end_date_before_close_uses_previous_day():
    """香港时区：收盘前只能查到前一交易日"""
    from api.services.sync_service import compute_market_end_date

    # 2026-09-18 周五 10:00 北京(=香港) 收盘前 → 前一天 0917
    assert compute_market_end_date(datetime(2026, 9, 18, 10, 0), "HK") == "20260917"


def test_hk_end_date_after_close_uses_today():
    """香港时区：收盘后允许查询当天"""
    from api.services.sync_service import compute_market_end_date

    # 2026-09-18 周五 19:00 北京(=香港) 收盘后 → 当天 0918
    assert compute_market_end_date(datetime(2026, 9, 18, 19, 0), "HK") == "20260918"


# ==================== 批量同步任务服务 ====================


def test_cn_syncer_routes_to_atomic_cn_pipeline(monkeypatch):
    from api.services.sync_service import CNSyncer
    from modules import data_sync

    client, lease, calls = object(), object(), []

    def sync(client_arg, code, start_date, end_date, *, lease=None):
        calls.append((client_arg, code, start_date, end_date, lease))
        return {"new_rows": 4, "indicator_rows": 4}

    monkeypatch.setattr(data_sync, "sync_cn_daily", sync)
    monkeypatch.setattr(data_sync, "sync_indicator_cache_incremental",
                        lambda code: (_ for _ in ()).throw(AssertionError("CN不得二次发布指标")))
    adapter = CNSyncer(client=client, lease=lease)
    assert adapter.atomic_indicators
    assert adapter.sync_daily_kline("600519.SH", "20261001", "20261020", raise_on_error=True) == 4
    assert adapter.sync_indicator_cache("600519.SH") == 4
    assert calls == [(client, "600519.SH", "20261001", "20261020", lease)]


def test_cn_atomic_indicator_stats_do_not_trigger_second_compute(temp_db, tmp_path):
    class AtomicSyncer:
        atomic_indicators = True

        def sync_indicator_cache(self, code):
            return 7

    service = _make_service(AtomicSyncer(), tmp_path)
    try:
        assert service._fill_indicators(AtomicSyncer(), "AAPL.US", added=0) == 7
    finally:
        service._executor.shutdown(wait=True)


def test_single_cn_sync_uses_atomic_adapter_and_bound_lease(temp_db, monkeypatch):
    from api.services import sync_service
    from api.services.sync_service import sync_workspace_stock
    from modules.database import init_database, market_context
    from modules.market_sync_lock import MarketSyncLease

    init_database("CN")
    calls = []

    class Adapter:
        def __init__(self, lease):
            calls.append(("lease", lease))

        def sync_daily_kline(self, code, start_date, end_date, raise_on_error):
            calls.append((code, start_date, end_date, raise_on_error))
            return 3

    monkeypatch.setattr(sync_service, "CNSyncer", Adapter)
    with market_context("CN"), MarketSyncLease.acquire("single-cn") as lease:
        assert sync_workspace_stock("600519.SH", days=30, indicators=True, lease=lease,
                                    now=datetime(2026, 9, 20, 10)) == 3
    assert calls[0] == ("lease", lease)
    assert calls[1] == ("600519.SH", "20260821", "20260918", True)


@pytest.mark.parametrize("code,indicators", [("AAPL.US", True), ("00700.HK", False)])
def test_single_us_sync_preserves_days_and_indicator_contract(temp_db, monkeypatch, code, indicators):
    from api.services.sync_service import sync_workspace_stock
    from modules import data_sync, yahoo_sync
    from modules.database import get_market, get_db_path
    from modules.market_sync_lock import MarketSyncLease

    calls = []
    monkeypatch.setattr(data_sync, "DataSyncer", lambda: pytest.fail("US单股同步不得初始化旧Tushare客户端"))
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TUSHARE_API_URL", raising=False)
    monkeypatch.setenv("DATA_MODE", "jnb")

    def daily(ts_code, start_date, end_date, **kwargs):
        assert get_market() == "US"
        assert kwargs["incremental"] is True
        calls.append(("daily", ts_code, start_date, end_date, str(get_db_path())))
        return 2

    def calculate(ts_code):
        calls.append(("indicators", ts_code))
        return 2

    monkeypatch.setattr(yahoo_sync, "sync_us_daily", daily)
    monkeypatch.setattr(data_sync, "sync_indicator_cache_incremental", calculate)
    with MarketSyncLease.acquire("single-us") as lease:
        assert sync_workspace_stock(code, days=30, indicators=indicators, lease=lease,
                                    now=datetime(2026, 9, 20, 10)) == 2
    expected = [("daily", code, "20260821", "20260918", str(get_db_path()))]
    if indicators:
        expected.append(("indicators", code))
    assert calls == expected


class FakeSyncer:
    """可控的假同步器：按队列返回结果或抛异常（接口与 YahooSyncer 一致）"""

    def __init__(self, outcomes=None, indicator_error=None, gate=None):
        # outcomes: dict[ts_code] -> list[int | Exception]，按调用顺序弹出
        self._outcomes = outcomes or {}
        self._indicator_error = indicator_error
        self._gate = gate  # threading.Event，设置前阻塞首只股票的日线同步
        self.daily_calls = []  # (ts_code, start_date, end_date, raise_on_error)
        self.indicator_calls = []  # (ts_code, days)

    def sync_daily_kline(self, ts_code, start_date=None, end_date=None, raise_on_error=False):
        if self._gate is not None and not self.daily_calls:
            assert self._gate.wait(timeout=5), "测试门闩超时"
        self.daily_calls.append((ts_code, start_date, end_date, raise_on_error))
        queue = self._outcomes.get(ts_code, [0])
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def sync_indicator_cache(self, ts_code):
        self.indicator_calls.append(ts_code)
        if self._indicator_error is not None:
            raise self._indicator_error
        return 1


FIXED_NOW = datetime(2026, 9, 20, 10, 0)  # 周日 10 点北京 → 美股/港股查询上界均为 20260918


def _make_service(syncer, tmp_path, sleeps=None, **kwargs):
    from api.services.sync_service import USStockSyncService

    sleep_recorder = sleeps if sleeps is not None else []
    return USStockSyncService(
        syncer=syncer,
        sleep=lambda s: sleep_recorder.append(s),
        now_provider=lambda: FIXED_NOW,
        backup_dir=tmp_path / "backups",
        **kwargs,
    )


def _run_and_wait(service, timeout=10):
    snapshot = service.submit()
    assert service.wait_for_completion(timeout=timeout), "任务未在预期时间内结束"
    return snapshot, service.get_status()


def _write_kline(conn, ts_code, trade_date):
    write_klines_to_db(
        conn,
        [{"ts_code": ts_code, "trade_date": trade_date, "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0}],
    )


def test_cn_batch_sync_uses_cn_adapter_and_registered_codes(temp_db, tmp_path, monkeypatch):
    from api.services import sync_service
    from api.services.sync_service import USStockSyncService
    from modules.data_sync import register_cn_assets
    from modules.database import get_connection, init_database, market_context
    from modules.market_sync_lock import MarketSyncLease

    init_database("CN")
    fake = FakeSyncer(outcomes={"600519.SH": [1]})
    leases = []

    def adapter(*, lease):
        assert isinstance(lease, MarketSyncLease)
        leases.append(lease)
        return fake

    monkeypatch.setattr(sync_service, "CNSyncer", adapter)
    with market_context("CN"), get_connection() as conn:
        register_cn_assets(conn, [{"ts_code": "600519.SH", "name": "贵州茅台", "market": "主板",
                                   "asset_type": "stock", "currency": "CNY", "sources": ["manual"]}])
    with market_context("CN"):
        service = USStockSyncService(now_provider=lambda: FIXED_NOW, backup_dir=tmp_path / "backups")
    try:
        _, final = _run_and_wait(service)
        assert final.workspace_market == "CN" and final.success == 1
        assert fake.daily_calls == [("600519.SH", "20240920", "20260918", True)]
        _, repeated = _run_and_wait(service)
        assert repeated.status == "completed"
        assert len(leases) == 2, "每个CN任务必须绑定本次持有的有效锁"
    finally:
        service._executor.shutdown(wait=True)


def test_sync_worker_pins_cn_identity_and_independent_lock(temp_db, tmp_path):
    from modules.database import get_connection, get_db_path, get_market, init_database, market_context
    from modules.market_sync_lock import MarketSyncLease

    init_database("CN")
    started, release = threading.Event(), threading.Event()
    observed = []
    with market_context("CN"):
        cn_path = get_db_path()
        service = _make_service(FakeSyncer(), tmp_path)

    def probe():
        observed.append((get_market(), get_db_path()))
        started.set()
        assert release.wait(5)
        with get_connection() as conn:
            conn.execute("INSERT INTO watchlist(ts_code,name) VALUES ('600519.SH','后台CN')")
        service._update(status="completed", phase="done")

    service._run_inner = probe
    try:
        with MarketSyncLease.acquire("us-running"):
            first = service.submit()
            assert started.wait(5)
            assert service.submit().task_id == first.task_id
            assert service.get_status().workspace_market == "CN"
            release.set()
            assert service.wait_for_completion(10)
        assert observed == [("CN", cn_path)]
        with get_connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
        with get_connection("CN") as conn:
            assert conn.execute("SELECT name FROM watchlist").fetchone()[0] == "后台CN"
    finally:
        release.set()
        service._executor.shutdown(wait=True)


def test_incremental_start_from_max_trade_date(temp_db, db_conn, tmp_path):
    """增量起点取真实 K 线最大日期 +1，不依赖 sync_log"""
    _write_kline(db_conn, "AAPL.US", "20260915")
    syncer = FakeSyncer(outcomes={"AAPL.US": [3]})

    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert syncer.daily_calls == [("AAPL.US", "20260916", "20260918", True)]
    assert final.status == "completed"
    assert final.success == 1
    assert final.new_rows == 3


def test_first_sync_uses_730_days_for_basic_only_stock(temp_db, db_conn, tmp_path):
    """仅有基本信息、无行情的股票首拉最近 730 个自然日"""
    write_stock_basic(db_conn, "AAPL.US", "苹果")
    syncer = FakeSyncer(outcomes={"AAPL.US": [500]})

    _run_and_wait(_make_service(syncer, tmp_path))

    expected_start = (FIXED_NOW - timedelta(days=730)).strftime("%Y%m%d")
    assert syncer.daily_calls == [("AAPL.US", expected_start, "20260918", True)]


def test_up_to_date_stock_skips_api_call(temp_db, db_conn, tmp_path):
    """库内数据已覆盖查询上界时不请求接口，直接计为无新增"""
    _write_kline(db_conn, "AAPL.US", "20260918")
    db_conn.execute(
        "INSERT INTO indicator_cache (ts_code, trade_date, close, open, high, low, vol, pct_chg) "
        "VALUES ('AAPL.US', '20260918', 1, 1, 1, 1, 1, 0)"
    )
    db_conn.commit()
    syncer = FakeSyncer()

    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert syncer.daily_calls == []
    assert final.status == "completed"
    assert final.no_change == 1


def test_no_new_data_counts_as_no_data(temp_db, db_conn, tmp_path):
    """接口成功返回空数据计为无新增，不是失败"""
    _write_kline(db_conn, "AAPL.US", "20260917")
    db_conn.execute(
        "INSERT INTO indicator_cache (ts_code, trade_date, close, open, high, low, vol, pct_chg) "
        "VALUES ('AAPL.US', '20260917', 1, 1, 1, 1, 1, 0)"
    )
    db_conn.commit()
    syncer = FakeSyncer(outcomes={"AAPL.US": [0]})

    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert final.status == "completed"
    assert final.no_data == 1
    assert final.no_change == 0
    assert final.failed == 0
    assert syncer.indicator_calls == []


def test_indicator_gap_triggers_incremental_compute(temp_db, db_conn, tmp_path):
    """无新增行情时，也会进入缺失日期补算入口。"""
    _write_kline(db_conn, "AAPL.US", "20260917")
    _write_kline(db_conn, "AAPL.US", "20260918")
    syncer = FakeSyncer(outcomes={"AAPL.US": [0]})

    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert syncer.indicator_calls == ["AAPL.US"]
    assert final.status == "completed"


def test_new_data_uses_incremental_indicators(temp_db, db_conn, tmp_path):
    """新行情只调用增量入口，不传入全历史重算天数。"""
    _write_kline(db_conn, "AAPL.US", "20260917")
    syncer = FakeSyncer(outcomes={"AAPL.US": [1]})

    _run_and_wait(_make_service(syncer, tmp_path))

    assert syncer.indicator_calls == ["AAPL.US"]


def test_failed_stock_not_counted_as_no_change(temp_db, db_conn, tmp_path):
    """同步异常重试耗尽后计为失败，不能混为无新增"""
    _write_kline(db_conn, "AAPL.US", "20260917")
    _write_kline(db_conn, "02331.HK", "20260918")
    syncer = FakeSyncer(outcomes={"02331.HK": [0], "AAPL.US": [Exception("connection reset")]})

    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert final.status == "partial_failure"
    assert final.failed == 1
    assert final.no_change == 1
    assert final.failures[0]["ts_code"] == "AAPL.US"
    assert "connection reset" in final.failures[0]["error"]


def test_rate_limit_retries_with_long_wait(temp_db, db_conn, tmp_path):
    """限流错误至少等待 65 秒后重试，重试成功仍计成功"""
    _write_kline(db_conn, "AAPL.US", "20260917")
    syncer = FakeSyncer(outcomes={"AAPL.US": [Exception("429 Too Many Requests"), 2]})
    sleeps = []

    _, final = _run_and_wait(_make_service(syncer, tmp_path, sleeps=sleeps))

    assert sleeps and sleeps[0] >= 65
    assert final.status == "completed"
    assert final.success == 1
    assert len(syncer.daily_calls) == 2


def test_duplicate_submit_returns_same_task(temp_db, db_conn, tmp_path):
    """任务运行中重复提交返回同一任务，不重复排队"""
    _write_kline(db_conn, "AAPL.US", "20260917")
    gate = threading.Event()
    syncer = FakeSyncer(outcomes={"AAPL.US": [1]}, gate=gate)
    service = _make_service(syncer, tmp_path)

    first = service.submit()
    second = service.submit()
    gate.set()
    assert service.wait_for_completion(timeout=10)

    assert first.task_id == second.task_id
    assert len(syncer.daily_calls) == 1  # 只执行了一次


def test_backup_created_before_sync(temp_db, db_conn, tmp_path):
    """写入前通过 SQLite backup API 生成备份文件"""
    _write_kline(db_conn, "AAPL.US", "20260917")
    syncer = FakeSyncer(outcomes={"AAPL.US": [1]})

    _run_and_wait(_make_service(syncer, tmp_path))

    backups = list((tmp_path / "backups").glob("us-hk-pre-sync-*.db"))
    assert len(backups) == 1
    backup = backups[0]
    import sqlite3

    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0] == 1


@pytest.mark.parametrize("market,prefix,code", [("US", "us-hk", "AAPL.US"), ("CN", "cn", "600519.SH")])
def test_sync_backup_labels_bound_market(temp_db, tmp_path, monkeypatch, market, prefix, code):
    import sqlite3
    from contextlib import closing
    from api.services import sync_service
    from modules.database import get_connection, init_database, market_context

    if market == "CN":
        init_database("CN")
    monkeypatch.setattr(sync_service, "collect_workspace_codes", lambda: [code])
    with market_context(market):
        with get_connection() as conn:
            conn.execute("INSERT INTO watchlist(ts_code,name) VALUES (?,?)", (code, market))
        service = _make_service(FakeSyncer(outcomes={code: [1]}), tmp_path)
    try:
        _, final = _run_and_wait(service)
        assert final.workspace_market == market
        backups = list((tmp_path / "backups").glob(f"{prefix}-pre-sync-*.db"))
        assert len(backups) == 1
        with closing(sqlite3.connect(backups[0])) as conn:
            assert conn.execute("SELECT ts_code,name FROM watchlist").fetchall() == [(code, market)]
    finally:
        service._executor.shutdown(wait=True)


def test_backup_failure_aborts_before_any_write(temp_db, db_conn, tmp_path, monkeypatch):
    """备份失败则停止任务，不写入任何数据"""
    from unittest.mock import Mock

    _write_kline(db_conn, "AAPL.US", "20260917")
    syncer = FakeSyncer(outcomes={"AAPL.US": [1]})

    from api.services import sync_service as module

    monkeypatch.setattr(module, "backup_database", Mock(side_effect=OSError("disk full")))
    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert final.status == "failed"
    assert "disk full" in final.message
    assert syncer.daily_calls == []


def test_submit_without_tushare_token_works(temp_db, db_conn, tmp_path):
    """美股/港股走 Yahoo，无需 TUSHARE_TOKEN 即可提交任务"""
    import os

    _write_kline(db_conn, "AAPL.US", "20260918")
    db_conn.execute(
        "INSERT INTO indicator_cache (ts_code, trade_date, close, open, high, low, vol, pct_chg) "
        "VALUES ('AAPL.US', '20260918', 1, 1, 1, 1, 1, 0)"
    )
    db_conn.commit()
    os.environ.pop("TUSHARE_TOKEN", None)
    syncer = FakeSyncer()

    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert final.status == "completed"
    assert final.no_change == 1


def test_real_incremental_sync_preserves_personal_data(temp_db, db_conn, tmp_path, monkeypatch):
    from api.services.sync_service import YahooSyncer
    from tests.test_yahoo_sync import FakeResponse, FakeSession, _chart_payload

    _write_kline(db_conn, "AAPL.US", "20260917")
    db_conn.execute("INSERT INTO watchlist(ts_code, notes) VALUES ('AAPL.US', '正式自选备注')")
    db_conn.execute(
        "INSERT INTO trade_records(ts_code, trade_date, action, price, quantity, amount, notes) "
        "VALUES ('AAPL.US', '20260917', 'buy', 100, 2, 200, '正式交易备注')"
    )
    db_conn.commit()
    tables = ("watchlist", "trade_records")
    before = {table: [tuple(row) for row in db_conn.execute(f"SELECT * FROM {table}")] for table in tables}
    payload = _chart_payload(
        [1789738200], {"open": [100], "high": [102], "low": [99], "close": [101], "volume": [100]}, -14400
    )
    syncer = YahooSyncer(session=FakeSession([FakeResponse(payload)]))
    monkeypatch.setattr(syncer, "sync_indicator_cache", lambda *args, **kwargs: 2)
    _, final = _run_and_wait(_make_service(syncer, tmp_path))
    assert final.status == "completed"
    assert final.new_rows == 1
    assert final.data_date == "20260918"
    for table in tables:
        assert [tuple(row) for row in db_conn.execute(f"SELECT * FROM {table}")] == before[table]


def test_indicator_failure_keeps_daily_count_and_continues(temp_db, db_conn, tmp_path):
    _write_kline(db_conn, "AAPL.US", "20260917")
    _write_kline(db_conn, "ADI.US", "20260917")
    syncer = FakeSyncer(outcomes={"AAPL.US": [1], "ADI.US": [2]}, indicator_error=ValueError("指标失败"))
    _, final = _run_and_wait(_make_service(syncer, tmp_path))
    assert final.processed == 2
    assert final.failed == 2
    assert final.new_rows == 3
    assert all("指标" in f["error"] for f in final.failures)


def test_proxy_failure_is_failed_and_safe_in_task_status(temp_db, db_conn, tmp_path, caplog):
    from api.services.sync_service import YahooSyncer
    from tests.test_yahoo_sync import FakeSession

    _write_kline(db_conn, "AAPL.US", "20260917")
    session = FakeSession([ConnectionError("http://private-user:private-password@invalid")] * 3)
    _, final = _run_and_wait(_make_service(YahooSyncer(session=session), tmp_path))
    assert final.status == "failed"
    assert final.failed == 1
    assert final.success == final.no_change == final.new_rows == 0
    assert len(session.calls) == 3
    assert "private-password" not in str(final.to_dict()) + caplog.text
    assert "private-user" not in str(final.to_dict()) + caplog.text
