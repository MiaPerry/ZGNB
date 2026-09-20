"""
A 股一键批量同步服务测试

覆盖：
- 股票名单并集与市场过滤
- 北京时间查询上界计算（收盘前/后、周末回退）
- 增量起点、无新增、失败区分、限流重试、鉴权终止
- 防重入、备份保护
- sync_daily_kline 的 raise_on_error 兼容行为
"""

import threading
from datetime import datetime
from unittest.mock import Mock

import pytest

from tests.conftest import write_klines_to_db, write_stock_basic


# ==================== 名单收集与市场过滤 ====================


def test_collect_codes_unions_stock_basic_and_kline(temp_db, db_conn):
    """stock_basic 与 daily_kline 取并集：有行情但缺基本信息的股票不能漏"""
    from api.services.sync_service import collect_a_share_codes

    write_stock_basic(db_conn, "600519.SH", "贵州茅台")
    write_klines_to_db(
        db_conn,
        [
            {"ts_code": "002415.SZ", "trade_date": "20260918", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0},
        ],
    )

    codes = collect_a_share_codes()
    assert codes == ["002415.SZ", "600519.SH"]


def test_collect_codes_excludes_us_and_hk(temp_db, db_conn):
    """排除美股/港股等非 A 股代码"""
    from api.services.sync_service import collect_a_share_codes

    write_klines_to_db(
        db_conn,
        [
            {"ts_code": "AAPL.US", "trade_date": "20260918", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0},
            {"ts_code": "02331.HK", "trade_date": "20260918", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0},
            {"ts_code": "300750.SZ", "trade_date": "20260918", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0},
        ],
    )

    assert collect_a_share_codes() == ["300750.SZ"]


def test_collect_codes_empty_db(temp_db):
    """空库返回空名单，不抛异常"""
    from api.services.sync_service import collect_a_share_codes

    assert collect_a_share_codes() == []


# ==================== 查询上界（北京时间） ====================


def test_query_end_date_after_close_uses_today():
    """18:00 之后允许查询当天完整日线"""
    from api.services.sync_service import compute_query_end_date

    # 2026-09-18 是周五
    assert compute_query_end_date(datetime(2026, 9, 18, 19, 30)) == "20260918"


def test_query_end_date_before_close_uses_previous_day():
    """18:00 之前只能查询到前一天"""
    from api.services.sync_service import compute_query_end_date

    assert compute_query_end_date(datetime(2026, 9, 18, 10, 0)) == "20260917"


def test_query_end_date_rolls_back_weekend():
    """周末向前回退到周五"""
    from api.services.sync_service import compute_query_end_date

    # 2026-09-20 周日 10 点 → 前一天周六 → 回退到周五 0918
    assert compute_query_end_date(datetime(2026, 9, 20, 10, 0)) == "20260918"
    # 2026-09-21 周一 10 点 → 前一天周日 → 回退到周五 0918
    assert compute_query_end_date(datetime(2026, 9, 21, 10, 0)) == "20260918"
    # 周六 19 点（收盘后）→ 当天周六 → 回退到周五
    assert compute_query_end_date(datetime(2026, 9, 19, 19, 0)) == "20260918"


# ==================== sync_daily_kline raise_on_error 兼容 ====================


def _make_syncer_with_boom():
    from modules.data_sync import DataSyncer

    syncer = DataSyncer(token="test-token")
    syncer.pro = Mock()
    syncer.pro.daily = Mock(side_effect=Exception("boom"))
    return syncer


def test_sync_daily_kline_swallows_error_by_default(temp_db):
    """旧行为兼容：默认异常时记录日志并返回 0"""
    syncer = _make_syncer_with_boom()
    assert syncer.sync_daily_kline("600519.SH") == 0


def test_sync_daily_kline_raise_on_error(temp_db):
    """raise_on_error=True 时透传异常，调用方可区分失败与无新增"""
    syncer = _make_syncer_with_boom()
    with pytest.raises(Exception, match="boom"):
        syncer.sync_daily_kline("600519.SH", raise_on_error=True)


# ==================== 批量同步任务服务 ====================


class FakeSyncer:
    """可控的假同步器：按队列返回结果或抛异常"""

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

    def sync_indicator_cache(self, ts_code, days=120):
        self.indicator_calls.append((ts_code, days))
        if self._indicator_error is not None:
            raise self._indicator_error
        return days


FIXED_NOW = datetime(2026, 9, 20, 10, 0)  # 周日 10 点 → 查询上界 20260918


def _make_service(syncer, tmp_path, sleeps=None, **kwargs):
    from api.services.sync_service import AShareSyncService

    sleep_recorder = sleeps if sleeps is not None else []
    return AShareSyncService(
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


def test_incremental_start_from_max_trade_date(temp_db, db_conn, tmp_path):
    """增量起点取真实 K 线最大日期 +1，不依赖 sync_log"""
    _write_kline(db_conn, "600519.SH", "20260915")
    syncer = FakeSyncer(outcomes={"600519.SH": [3]})

    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert syncer.daily_calls == [("600519.SH", "20260916", "20260918", True)]
    assert final.status == "completed"
    assert final.success == 1
    assert final.new_rows == 3


def test_first_sync_uses_730_days_for_basic_only_stock(temp_db, db_conn, tmp_path):
    """仅有基本信息、无行情的股票首拉最近 730 个自然日"""
    write_stock_basic(db_conn, "600519.SH", "贵州茅台")
    syncer = FakeSyncer(outcomes={"600519.SH": [500]})

    _run_and_wait(_make_service(syncer, tmp_path))

    from datetime import timedelta

    expected_start = (FIXED_NOW - timedelta(days=730)).strftime("%Y%m%d")
    assert syncer.daily_calls == [("600519.SH", expected_start, "20260918", True)]


def test_up_to_date_stock_skips_api_call(temp_db, db_conn, tmp_path):
    """库内数据已覆盖查询上界时不请求接口，直接计为无新增"""
    _write_kline(db_conn, "600519.SH", "20260918")
    # 指标数与 K 线数一致，无需重算
    db_conn.execute(
        "INSERT INTO indicator_cache (ts_code, trade_date, close, open, high, low, vol, pct_chg) "
        "VALUES ('600519.SH', '20260918', 1, 1, 1, 1, 1, 0)"
    )
    db_conn.commit()
    syncer = FakeSyncer()

    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert syncer.daily_calls == []
    assert final.status == "completed"
    assert final.no_change == 1


def test_no_new_data_counts_as_no_change(temp_db, db_conn, tmp_path):
    """接口成功返回空数据计为无新增，不是失败"""
    _write_kline(db_conn, "600519.SH", "20260917")
    db_conn.execute(
        "INSERT INTO indicator_cache (ts_code, trade_date, close, open, high, low, vol, pct_chg) "
        "VALUES ('600519.SH', '20260917', 1, 1, 1, 1, 1, 0)"
    )
    db_conn.commit()
    syncer = FakeSyncer(outcomes={"600519.SH": [0]})

    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert final.status == "completed"
    assert final.no_change == 1
    assert final.failed == 0
    assert syncer.indicator_calls == []


def test_indicator_mismatch_triggers_recompute(temp_db, db_conn, tmp_path):
    """指标缓存条数与 K 线不一致时，即使无新增也重算指标"""
    _write_kline(db_conn, "600519.SH", "20260917")
    _write_kline(db_conn, "600519.SH", "20260918")
    syncer = FakeSyncer(outcomes={"600519.SH": [0]})

    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert syncer.indicator_calls == [("600519.SH", 2)]  # days=该股票全部 K 线条数
    assert final.status == "completed"


def test_new_data_recomputes_indicators_with_full_history(temp_db, db_conn, tmp_path):
    """有新增行情时按全部历史条数重算指标，预热递推指标"""
    _write_kline(db_conn, "600519.SH", "20260917")
    syncer = FakeSyncer(outcomes={"600519.SH": [1]})

    _run_and_wait(_make_service(syncer, tmp_path))

    # 新行情实际由 FakeSyncer 模拟“已写入”，此处只验证 days 使用当时库内条数
    assert syncer.indicator_calls == [("600519.SH", 1)]


def test_failed_stock_not_counted_as_no_change(temp_db, db_conn, tmp_path):
    """同步异常重试耗尽后计为失败，不能混为无新增"""
    _write_kline(db_conn, "600519.SH", "20260917")
    _write_kline(db_conn, "300750.SZ", "20260918")
    syncer = FakeSyncer(outcomes={"300750.SZ": [0], "600519.SH": [Exception("connection reset")]})

    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert final.status == "partial_failure"
    assert final.failed == 1
    assert final.no_change == 1
    assert final.failures[0]["ts_code"] == "600519.SH"
    assert "connection reset" in final.failures[0]["error"]


def test_rate_limit_retries_with_long_wait(temp_db, db_conn, tmp_path):
    """限流错误至少等待 65 秒后重试，重试成功仍计成功"""
    _write_kline(db_conn, "600519.SH", "20260917")
    syncer = FakeSyncer(outcomes={"600519.SH": [Exception("抱歉，您每分钟最多访问 daily 接口 50 次"), 2]})
    sleeps = []

    _, final = _run_and_wait(_make_service(syncer, tmp_path, sleeps=sleeps))

    assert sleeps and sleeps[0] >= 65
    assert final.status == "completed"
    assert final.success == 1
    assert len(syncer.daily_calls) == 2


def test_auth_error_aborts_task_immediately(temp_db, db_conn, tmp_path):
    """鉴权/配置错误直接终止任务，不对全部股票重复无效请求"""
    _write_kline(db_conn, "300750.SZ", "20260917")
    _write_kline(db_conn, "600519.SH", "20260917")
    syncer = FakeSyncer(
        outcomes={"300750.SZ": [Exception("token 无效，请检查权限")], "600519.SH": [1]}
    )

    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert final.status == "failed"
    assert len(syncer.daily_calls) == 1  # 第一只鉴权失败后不再请求
    assert "token" in final.message or "权限" in final.message


def test_duplicate_submit_returns_same_task(temp_db, db_conn, tmp_path):
    """任务运行中重复提交返回同一任务，不重复排队"""
    _write_kline(db_conn, "600519.SH", "20260917")
    gate = threading.Event()
    syncer = FakeSyncer(outcomes={"600519.SH": [1]}, gate=gate)
    service = _make_service(syncer, tmp_path)

    first = service.submit()
    second = service.submit()
    gate.set()
    assert service.wait_for_completion(timeout=10)

    assert first.task_id == second.task_id
    assert len(syncer.daily_calls) == 1  # 只执行了一次


def test_backup_created_before_sync(temp_db, db_conn, tmp_path):
    """写入前通过 SQLite backup API 生成备份文件"""
    _write_kline(db_conn, "600519.SH", "20260917")
    syncer = FakeSyncer(outcomes={"600519.SH": [1]})

    _run_and_wait(_make_service(syncer, tmp_path))

    backup = tmp_path / "backups" / "a-share-pre-sync.db"
    assert backup.exists()
    import sqlite3

    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0] == 1


def test_backup_failure_aborts_before_any_write(temp_db, db_conn, tmp_path, monkeypatch):
    """备份失败则停止任务，不写入任何数据"""
    _write_kline(db_conn, "600519.SH", "20260917")
    syncer = FakeSyncer(outcomes={"600519.SH": [1]})

    from api.services import sync_service as module

    monkeypatch.setattr(module, "backup_database", Mock(side_effect=OSError("disk full")))
    _, final = _run_and_wait(_make_service(syncer, tmp_path))

    assert final.status == "failed"
    assert "disk full" in final.message
    assert syncer.daily_calls == []


def test_missing_config_raises_clear_error(tmp_path):
    """缺少 TUSHARE_TOKEN 时提交任务返回明确配置错误"""
    import os

    from api.services.sync_service import AShareSyncService, SyncConfigError

    os.environ.pop("TUSHARE_TOKEN", None)
    service = AShareSyncService(backup_dir=tmp_path / "backups")
    with pytest.raises(SyncConfigError):
        service.submit()
