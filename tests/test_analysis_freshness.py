"""增量更新后的分析、图表及跨进程缓存契约。"""

import pytest


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
