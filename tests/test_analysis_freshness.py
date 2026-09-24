"""增量更新后的分析、图表及跨进程缓存契约。"""

import math

import pandas as pd
import pytest


def _brick_chart(db_conn, monkeypatch, closes, days=120):
    """用隔离数据库中的真实 OHLC 走图表入口，仅隔离无关的策略扫描。"""
    import modules.strategies
    from api.services.stock_service import get_kline_chart_data
    from tests.conftest import write_klines_to_db
    from tests.test_indicator_cache import _make_daily_klines

    bars = _make_daily_klines("BRICK.US", len(closes))
    for bar, close in zip(bars, closes):
        bar.open = bar.close = close
        bar.high, bar.low = 110.0, 90.0
        bar.amount = close * bar.vol
    write_klines_to_db(db_conn, [vars(bar) for bar in bars])
    monkeypatch.setattr(modules.strategies, "detect_all_strategies", lambda *a, **k: [])
    return get_kline_chart_data("BRICK.US", days)


@pytest.mark.parametrize("length", [1, 9, 10, 11, 12, 13, 69])
def test_chart_brick_warmup_is_missing(db_conn, monkeypatch, length):
    """前11根尚不可计算，不得把占位0作为有效砖值和砖色。"""
    chart = _brick_chart(db_conn, monkeypatch, [100.0] * length)
    warmup = min(length, 11)
    assert len(chart["brick"]["values"]) == len(chart["dates"]) == length
    assert chart["brick"]["values"] == [None] * warmup + [86.0] * (length - warmup)
    assert chart["brick"]["colors"] == [None] * warmup + [1] * (length - warmup)


def test_chart_keeps_mature_zero_bricks(db_conn, monkeypatch):
    """第12根以后收盘价持续位于区间底部，真实砖值0必须保留。"""
    chart = _brick_chart(db_conn, monkeypatch, [90.0] * 15)
    assert chart["brick"]["values"][11:] == [0.0] * 4
    assert chart["brick"]["colors"][11:] == [1] * 4


def test_chart_keeps_zero_transitions_and_colors(db_conn, monkeypatch):
    """真实砖值正转零、连续零、零转正仍按前一期大小着色。"""
    chart = _brick_chart(db_conn, monkeypatch, [110.0] * 12 + [90.0] * 60 + [110.0] * 2)
    values, colors = chart["brick"]["values"], chart["brick"]["colors"]
    first_zero = next(i for i in range(12, len(values)) if values[i] == 0)
    assert values[first_zero - 1] > 0
    assert colors[first_zero] == -1
    assert values[first_zero + 1] == 0
    assert colors[first_zero + 1] == 1
    assert values[71] == 0 and values[72] > 0
    assert colors[72] == 1


def test_chart_warmup_uses_available_history_not_display_length(db_conn, monkeypatch):
    """只显示5根时，仍应利用显示范围外的历史计算有效砖值。"""
    chart = _brick_chart(db_conn, monkeypatch, [100.0] * 30, days=5)
    assert len(chart["dates"]) == 5
    assert chart["brick"]["values"] == [86.0] * 5
    assert chart["brick"]["colors"] == [1] * 5


def test_double_lines_and_crosses_match_reference_across_entries(db_conn, monkeypatch):
    """同一历史起点下，以独立公式验证现算、快照、图表及交叉日期。"""
    import modules.strategies
    from api.services.stock_service import get_kline_chart_data
    from modules.data_sync import sync_indicator_cache_incremental
    from modules.indicators import data_layer, detect_double_line_cross
    from tests.conftest import write_klines_to_db
    from tests.test_indicator_cache import _make_daily_klines

    code = "LINES.US"
    bars = _make_daily_klines(code, 180)
    for i, bar in enumerate(bars):
        price = 100 + 15 * math.sin(i / 4)
        bar.open = bar.close = price
        bar.high, bar.low = price + 2, price - 2
        bar.amount = price * bar.vol
    write_klines_to_db(db_conn, [vars(bar) for bar in bars])
    monkeypatch.setattr(modules.strategies, "detect_all_strategies", lambda *a, **k: [])

    closes = pd.Series([bar.close for bar in bars])
    white = closes.ewm(span=10, adjust=False).mean().ewm(span=10, adjust=False).mean().map(lambda v: round(v, 2))
    yellow = (sum(closes.rolling(period).mean() for period in (14, 28, 57, 114)) / 4).map(lambda v: round(v, 2))
    gold = (white.shift(1) <= yellow.shift(1)) & (white > yellow)
    dead = (white.shift(1) >= yellow.shift(1)) & (white < yellow)
    gold.iloc[:115] = False
    dead.iloc[:115] = False
    assert gold.any() and dead.any(), "样本必须覆盖实际金叉与死叉，不能只比较全False"

    data_layer.clear_indicator_memory_cache()
    try:
        uncached = data_layer.analyze_stock(code, days=len(bars))
        assert (uncached.zg_white, uncached.dg_yellow) == (white.iloc[-1], yellow.iloc[-1])
        assert (uncached.is_gold_cross, uncached.is_dead_cross) == (bool(gold.iloc[-1]), bool(dead.iloc[-1]))
        assert db_conn.execute("SELECT COUNT(*) FROM indicator_cache").fetchone()[0] == 0

        assert sync_indicator_cache_incremental(code) == len(bars)
        snapshots = db_conn.execute(
            "SELECT trade_date, zg_white, dg_yellow, is_gold_cross, is_dead_cross "
            "FROM indicator_cache WHERE ts_code=? ORDER BY trade_date", (code,)
        ).fetchall()
        for i in range(114, len(bars)):
            row = snapshots[i]
            expected_cross = (bool(gold.iloc[i]), bool(dead.iloc[i]))
            assert row["trade_date"] == bars[i].trade_date
            assert (row["zg_white"], row["dg_yellow"]) == (white.iloc[i], yellow.iloc[i])
            assert (bool(row["is_gold_cross"]), bool(row["is_dead_cross"])) == expected_cross
            assert detect_double_line_cross(bars[:i + 1]) == expected_cross

        for days in (10, 120, 500):
            data_layer.clear_indicator_memory_cache()
            cached = data_layer.analyze_stock(code, days=days)
            assert (cached.zg_white, cached.dg_yellow) == (white.iloc[-1], yellow.iloc[-1])
            assert (cached.is_gold_cross, cached.is_dead_cross) == (bool(gold.iloc[-1]), bool(dead.iloc[-1]))
            chart = get_kline_chart_data(code, days)
            offset = max(0, len(bars) - days)
            assert chart["dates"] == [bar.trade_date for bar in bars[offset:]]
            assert chart["overlays"]["white_line"] == [
                None if i < 9 else white.iloc[i] for i in range(offset, len(bars))
            ]
            assert chart["overlays"]["yellow_line"] == [
                None if i < 113 else yellow.iloc[i] for i in range(offset, len(bars))
            ]
        assert snapshots == db_conn.execute(
            "SELECT trade_date, zg_white, dg_yellow, is_gold_cross, is_dead_cross "
            "FROM indicator_cache WHERE ts_code=? ORDER BY trade_date", (code,)
        ).fetchall()
    finally:
        data_layer.clear_indicator_memory_cache()


def test_analysis_and_chart_share_warmed_basics_without_writing(db_conn, monkeypatch):
    from modules.indicators import data_layer
    from modules.data_sync import sync_indicator_cache_incremental
    from api.services.stock_service import get_kline_chart_data
    from tests.conftest import write_klines_to_db
    from tests.test_indicator_cache import _make_daily_klines
    import modules.strategies

    bars = _make_daily_klines('ADI.US', 260)
    write_klines_to_db(db_conn, [vars(b) for b in bars])
    monkeypatch.setattr(modules.strategies, 'detect_all_strategies', lambda *a, **k: [])
    uncached = data_layer.analyze_stock('ADI.US', days=120)
    assert db_conn.execute('SELECT COUNT(*) FROM indicator_cache').fetchone()[0] == 0
    sync_indicator_cache_incremental('ADI.US')
    before = [tuple(r) for r in db_conn.execute('SELECT * FROM indicator_cache')]
    cached = data_layer.analyze_stock('ADI.US', days=120)
    assert cached.sell_items == uncached.sell_items and cached.sell_items
    assert cached.high_52w == max(b.high for b in bars[-240:])
    assert cached.macd_veto == uncached.macd_veto
    for days in (10, 120, 500):
        chart = get_kline_chart_data('ADI.US', days)
        result = data_layer.analyze_stock('ADI.US', days)
        assert chart['macd']['hist'][-1] == pytest.approx(result.macd_hist, abs=0.0001)
        assert chart['kdj']['j'][-1] == pytest.approx(result.j, abs=0.01)
        assert chart['overlays']['ma60'][-1] == pytest.approx(result.ma60, abs=0.01)
        assert result.j == cached.j
        assert len(chart['overlays']['white_line']) == len(chart['dates'])
        assert chart['overlays']['white_line'][-1] is not None and chart['overlays']['white_line'][-1] > 0
        assert chart['overlays']['yellow_line'][-1] is not None and chart['overlays']['yellow_line'][-1] > 0
    assert [tuple(r) for r in db_conn.execute('SELECT * FROM indicator_cache')] == before


def test_cache_rechecks_persistent_revision(db_conn):
    from modules.indicators import data_layer
    from modules.data_freshness import record_data_change
    from tests.test_indicator_cache import _make_indicator_result, _make_daily_klines

    result = _make_indicator_result()
    data_layer._save_indicator_cache(result, _make_daily_klines())
    assert data_layer._load_indicator_cache(result.ts_code, result.trade_date).k == 30
    db_conn.execute('UPDATE indicator_cache SET k=75')
    record_data_change(db_conn, result.ts_code, result.trade_date)
    db_conn.commit()
    assert data_layer._load_indicator_cache(result.ts_code, result.trade_date).k == 75
