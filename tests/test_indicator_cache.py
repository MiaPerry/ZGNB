"""
缓存层单元测试
测试 indicator_cache 的保存、加载、内存缓存功能
"""

from datetime import datetime, timedelta

from modules.indicators import (
    IndicatorResult,
    DailyData,
    TradeSignal,
    _save_indicator_cache,
    _load_indicator_cache,
    clear_indicator_memory_cache,
    _indicator_memory_cache,
)


def _make_daily_klines(ts_code="600519.SH", n=5, start_price=100.0):
    """生成测试用的 DailyData 列表"""
    klines = []
    dt = datetime(2026, 1, 1)
    price = start_price
    for i in range(n):
        date_str = dt.strftime("%Y%m%d")
        prev_close = price
        price *= 1.01
        klines.append(
            DailyData(
                ts_code=ts_code,
                trade_date=date_str,
                open=prev_close,
                high=price * 1.02,
                low=prev_close * 0.98,
                close=price,
                vol=10000 + i * 100,
                amount=price * (10000 + i * 100),
                pct_chg=1.0,
                prev_close=prev_close,
            )
        )
        dt += timedelta(days=1)
    return klines


def _make_indicator_result(ts_code="600519.SH", trade_date="20260105"):
    """生成测试用的 IndicatorResult"""
    return IndicatorResult(
        ts_code=ts_code,
        trade_date=trade_date,
        k=30.0,
        d=25.0,
        j=40.0,
        dif=0.5,
        dea=0.3,
        macd_hist=0.4,
        bbi=105.0,
        ma5=102.0,
        ma10=101.0,
        ma20=100.0,
        ma60=98.0,
        rsi6=55.0,
        rsi12=52.0,
        rsi24=50.0,
        wr5=-30.0,
        wr10=-40.0,
        boll_mid=100.0,
        boll_upper=110.0,
        boll_lower=90.0,
        boll_width=20.0,
        boll_position=50.0,
        vol_ratio=1.2,
        brick_value=10.0,
        brick_trend="RED",
        brick_count=3,
        sell_score=4,
        signal=TradeSignal.B1,
        prev_high=104.0,
        prev_low=99.0,
        dmi_plus=25.0,
        dmi_minus=20.0,
        adx=22.0,
    )


def test_batch_brick_history_reuses_daily_values(temp_db, db_conn, monkeypatch):
    """逐日结果与原公式完全相同，但批量计算不重复遍历所有历史前缀。"""
    import math
    import modules.indicators as indicators
    from modules.data_sync import DataSyncer
    from tests.conftest import write_klines_to_db

    rows = []
    for i in range(160):
        price = 100 + 15 * math.sin(i / 4)
        rows.append({"ts_code": "ADI.US", "trade_date": (datetime(2025, 1, 1) + timedelta(days=i)).strftime("%Y%m%d"),
                     "open": price, "high": price + 2, "low": price - 2, "close": price,
                     "vol": 1000, "amount": price * 1000, "pct_chg": 0})
    write_klines_to_db(db_conn, rows)
    klines = indicators.get_kline_data("ADI.US", 160)
    expected = [indicators.calculate_brick_history(klines[:i + 1]) for i in range(160)]
    expected_cross = [indicators.detect_double_line_cross(klines[:i + 1]) for i in range(160)]

    def no_nested_history(*args, **kwargs):
        raise AssertionError("不应重复计算历史前缀")

    monkeypatch.setattr(indicators, "calculate_brick_history", no_nested_history)
    monkeypatch.setattr(indicators, "detect_double_line_cross", no_nested_history)
    assert DataSyncer().sync_indicator_cache("ADI.US", days=160) == 160
    actual = db_conn.execute("SELECT brick_trend, brick_count FROM indicator_cache ORDER BY trade_date").fetchall()
    assert [tuple(row) for row in actual] == expected
    crosses = db_conn.execute("SELECT is_gold_cross, is_dead_cross FROM indicator_cache ORDER BY trade_date").fetchall()
    assert [tuple(row) for row in crosses] == expected_cross


def test_incremental_only_builds_missing_rows(db_conn, monkeypatch):
    """已有历史逐字保留，尾部和中间缺口只各计算一次。"""
    import modules.data_sync as sync
    from modules.indicators import cache_builder
    from tests.conftest import generate_uptrend_klines, write_klines_to_db

    rows = generate_uptrend_klines(n=125, ts_code="ADI.US")
    write_klines_to_db(db_conn, rows)
    assert sync.sync_indicator_cache_incremental("ADI.US") == 125
    original = {r["trade_date"]: tuple(r) for r in db_conn.execute("SELECT * FROM indicator_cache")}
    missing = [rows[50]["trade_date"], rows[-1]["trade_date"]]
    db_conn.executemany("DELETE FROM indicator_cache WHERE trade_date=?", [(d,) for d in missing])
    db_conn.commit()
    calls = []
    build = cache_builder.IndicatorContext.row

    def counted(self, i):
        calls.append(i)
        return build(self, i)

    monkeypatch.setattr(cache_builder.IndicatorContext, "row", counted)
    assert sync.sync_indicator_cache_incremental("ADI.US") == 2
    assert calls == [50, 124]
    actual = {r["trade_date"]: tuple(r) for r in db_conn.execute("SELECT * FROM indicator_cache")}
    assert actual == original
    calls.clear()
    assert sync.sync_indicator_cache_incremental("ADI.US") == 0
    assert calls == []


def test_incremental_matches_prefix_formulas_and_reuses_sequences(db_conn, monkeypatch):
    import math
    import pytest
    import modules.indicators as ind
    from modules.indicators.cache_builder import IndicatorContext
    from modules.indicators import core, volume_patterns
    from modules.data_sync import sync_indicator_cache_incremental
    from tests.conftest import write_klines_to_db

    bars = _make_daily_klines("ADI.US", n=170)
    for i, bar in enumerate(bars):
        price = 100 + math.sin(i / 4) * 15
        bar.open, bar.close, bar.high, bar.low = price - 1, price, price + 2, price - 2
    context = IndicatorContext(bars)
    for i in list(range(35)) + [50, 114, 115, 120, 169]:
        prefix = bars[:i + 1]
        row = context.row(i)
        assert (row["k"], row["d"], row["j"]) == ind.calculate_kdj(prefix)
        assert row["brick_value"] == ind.calculate_brick_value(prefix)
        assert (row["brick_trend"], row["brick_count"]) == ind.calculate_brick_history(prefix)
        assert (row["is_gold_cross"], row["is_dead_cross"]) == ind.detect_double_line_cross(prefix)
        assert row["signal"] == ind.detect_trade_signal(prefix).value
        if i >= 24:
            assert (row["rsi6"], row["rsi12"], row["rsi24"]) == ind.calculate_rsi_multi(prefix)
        if i >= 33:
            expected = ind.calculate_macd(prefix)
            assert (row["dif"], row["dea"], row["macd_hist"]) == pytest.approx([s[-1] for s in expected])

    write_klines_to_db(db_conn, [vars(b) for b in bars[:-1]])
    sync_indicator_cache_incremental("ADI.US")
    old = [tuple(r) for r in db_conn.execute("SELECT * FROM indicator_cache ORDER BY trade_date")]
    write_klines_to_db(db_conn, [vars(bars[-1])])
    calls = []
    for name in ("precompute_kdj_sequence", "precompute_macd_sequence"):
        original = getattr(core, name)
        def counted(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(core, name, counted)
    def forbidden(*args, **kwargs):
        raise AssertionError("下游不得重复预热")
    monkeypatch.setattr(volume_patterns, "calculate_kdj", forbidden)
    monkeypatch.setattr(volume_patterns, "calculate_macd", forbidden)
    assert sync_indicator_cache_incremental("ADI.US") == 1
    assert calls == ["precompute_kdj_sequence", "precompute_macd_sequence"]
    assert [tuple(r) for r in db_conn.execute("SELECT * FROM indicator_cache ORDER BY trade_date")][:-1] == old


def test_incremental_failure_keeps_existing_history(db_conn, monkeypatch):
    import pytest
    from modules.data_sync import sync_indicator_cache_incremental
    from modules.indicators.cache_builder import IndicatorContext
    from tests.conftest import generate_uptrend_klines, write_klines_to_db

    write_klines_to_db(db_conn, generate_uptrend_klines(n=12, ts_code="ADI.US"))
    original = IndicatorContext.row
    def fail(self, i):
        if i == 10:
            raise ValueError("计算失败")
        return original(self, i)
    monkeypatch.setattr(IndicatorContext, "row", fail)
    with pytest.raises(ValueError, match="计算失败"):
        sync_indicator_cache_incremental("ADI.US")
    assert db_conn.execute("SELECT COUNT(*) FROM indicator_cache").fetchone()[0] == 0


def test_cache_rechecks_persistent_revision(db_conn):
    from modules.data_freshness import record_data_change
    result = _make_indicator_result()
    _save_indicator_cache(result, _make_daily_klines())
    assert _load_indicator_cache(result.ts_code, result.trade_date).k == 30
    db_conn.execute('UPDATE indicator_cache SET k=75')
    record_data_change(db_conn, result.ts_code, result.trade_date)
    db_conn.commit()
    assert _load_indicator_cache(result.ts_code, result.trade_date).k == 75


class TestIndicatorCache:
    def setup_method(self):
        """每个测试方法前清理内存缓存"""
        clear_indicator_memory_cache()

    def teardown_method(self):
        """每个测试方法后清理内存缓存"""
        clear_indicator_memory_cache()

    def test_save_and_load_from_db(self, db_conn):
        """保存到数据库后，能从数据库正确加载"""
        klines = _make_daily_klines()
        result = _make_indicator_result()

        # 保存
        success = _save_indicator_cache(result, klines)
        assert success is True

        # 清空内存缓存，强制从数据库加载
        clear_indicator_memory_cache()

        loaded = _load_indicator_cache(result.ts_code, result.trade_date)
        assert loaded is not None
        assert loaded.ts_code == result.ts_code
        assert loaded.trade_date == result.trade_date
        assert loaded.k == result.k
        assert loaded.d == result.d
        assert loaded.j == result.j
        assert loaded.dif == result.dif
        assert loaded.dea == result.dea
        assert loaded.macd_hist == result.macd_hist
        assert loaded.bbi == result.bbi
        assert loaded.signal == result.signal

    def test_load_from_memory_cache(self, db_conn):
        """保存后，优先从内存缓存加载"""
        klines = _make_daily_klines()
        result = _make_indicator_result()

        # 保存（会写入内存缓存）
        _save_indicator_cache(result, klines)

        # 直接加载，应该命中内存缓存
        loaded = _load_indicator_cache(result.ts_code, result.trade_date)
        assert loaded is not None
        assert loaded.k == result.k

    def test_load_miss(self, db_conn):
        """未保存的数据，加载应返回 None"""
        loaded = _load_indicator_cache("999999.XSHE", "20260101")
        assert loaded is None

    def test_clear_memory_cache(self, db_conn):
        """清空内存缓存后，应从数据库重新加载"""
        klines = _make_daily_klines()
        result = _make_indicator_result()

        # 保存到数据库和内存缓存
        _save_indicator_cache(result, klines)

        # 确认内存缓存存在
        assert (result.ts_code, result.trade_date) in _indicator_memory_cache

        # 清空内存缓存
        clear_indicator_memory_cache()

        # 内存缓存应已清空
        assert (result.ts_code, result.trade_date) not in _indicator_memory_cache

        # 仍能从数据库加载
        loaded = _load_indicator_cache(result.ts_code, result.trade_date)
        assert loaded is not None
        assert loaded.k == result.k

    def test_load_populates_memory_cache(self, db_conn):
        """从数据库加载后，应自动写入内存缓存"""
        klines = _make_daily_klines()
        result = _make_indicator_result()

        # 保存并清空内存缓存
        _save_indicator_cache(result, klines)
        clear_indicator_memory_cache()

        # 确认内存缓存为空
        assert (result.ts_code, result.trade_date) not in _indicator_memory_cache

        # 从数据库加载
        loaded = _load_indicator_cache(result.ts_code, result.trade_date)
        assert loaded is not None

        # 加载后应自动写入内存缓存
        assert (result.ts_code, result.trade_date) in _indicator_memory_cache
        assert _indicator_memory_cache[(result.ts_code, result.trade_date)].k == result.k

    def test_save_overwrite(self, db_conn):
        """重复保存应覆盖旧数据"""
        klines = _make_daily_klines()
        result1 = _make_indicator_result()
        result1.k = 30.0

        result2 = _make_indicator_result()
        result2.k = 80.0

        # 第一次保存
        _save_indicator_cache(result1, klines)

        # 第二次保存（覆盖）
        _save_indicator_cache(result2, klines)

        # 清空内存缓存，强制从数据库加载
        clear_indicator_memory_cache()

        loaded = _load_indicator_cache(result1.ts_code, result1.trade_date)
        assert loaded.k == 80.0

    def test_save_without_klines(self, db_conn):
        """klines 为空时应返回 False"""
        result = _make_indicator_result()
        success = _save_indicator_cache(result, [])
        assert success is False
