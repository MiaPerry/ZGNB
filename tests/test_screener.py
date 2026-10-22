"""
screener.py 选股测试
"""

from modules.screener import (
    StockScore,
    MarketStatus,
    calculate_ma,
    calculate_vol_ma,
    calculate_kdj,
    calculate_bbi,
    is_perfect_pattern,
    score_b1_opportunity,
    score_trend,
    score_volume_pattern,
    score_risk,
)
from tests.conftest import generate_uptrend_klines, generate_downtrend_klines


def test_screen_dispatches_pinned_market_and_path_to_pool(temp_db, monkeypatch):
    import concurrent.futures
    from modules import screener
    from modules.database import get_db_path, market_context

    submitted = []

    class RecordingPool:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, function, *args):
            submitted.append(args)
            future = concurrent.futures.Future()
            future.set_result(None)
            return future

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", RecordingPool)
    monkeypatch.setattr(screener, "_PARALLEL_THRESHOLD", 1)
    monkeypatch.setattr(screener, "get_all_stocks", lambda: [{"ts_code": "600519.SH"}])
    with market_context("CN"):
        path = str(get_db_path())
        assert screener.screen_stocks() == []
    assert submitted == [("600519.SH", "CN", path)]


def test_process_worker_uses_explicit_cn_binding(temp_db):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    from modules.database import get_connection, get_db_path, init_database, market_context
    from modules.screener import _analyze_worker
    from tests.conftest import write_klines_to_db

    code = "600519.SH"
    with get_connection() as conn:
        write_klines_to_db(conn, generate_uptrend_klines(n=35, ts_code=code, start_price=10))
    init_database("CN")
    with market_context("CN"):
        path = str(get_db_path())
        with get_connection() as conn:
            write_klines_to_db(conn, generate_uptrend_klines(n=35, ts_code=code, start_price=2000))
    # 真正的spawn子进程不继承父线程ContextVar，只能依赖显式任务参数。
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        result = pool.submit(_analyze_worker, code, "CN", path).result(timeout=30)
    assert result is not None
    assert result[0] == code
    assert min(bar.close for bar in result[1]) > 1000


def test_cn_worker_reads_score_and_bars_from_same_snapshot(temp_db, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from modules import screener, database, data_freshness
    from tests.conftest import write_klines_to_db

    code = "600519.SH"
    with database.market_context("CN") as binding:
        database.init_database()
        with database.get_connection() as conn:
            write_klines_to_db(conn, generate_uptrend_klines(n=35, ts_code=code))
            conn.execute("INSERT INTO stock_basic(ts_code,name) VALUES (?, '旧名称')", (code,))
            data_freshness.record_data_change(conn, code, "20260101")
        version = data_freshness.get_data_version(code)

        def score(ts_code, bars):
            def publish():
                with database.market_context("CN", binding.db_path), database.get_connection() as conn:
                    conn.execute("UPDATE stock_basic SET name='新名称'")
                    data_freshness.record_data_change(conn, code, "20260101")
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(publish).result(timeout=5)
            with database.get_connection() as conn:
                name = conn.execute("SELECT name FROM stock_basic").fetchone()[0]
            assert name == "旧名称" and data_freshness.get_data_version(code) == version
            return StockScore(ts_code=ts_code, name=name)

        monkeypatch.setattr(screener, "analyze_stock", score)
        assert screener._analyze_worker(code, "CN", binding.db_path)[2].name == "旧名称"


def _cn_pool(conn):
    from modules.data_sync import register_cn_assets

    register_cn_assets(conn, [
        {"ts_code": "600519.SH", "name": "贵州茅台", "market": "主板", "asset_type": "stock"},
        {"ts_code": "000001.SZ", "name": "平安银行", "market": "A股", "asset_type": "stock"},
        {"ts_code": "688981.SH", "name": "中芯国际", "market": "科创板", "asset_type": "stock"},
        {"ts_code": "000300.SH", "name": "沪深300", "market": "指数", "asset_type": "index"},
    ])


def test_cn_pool_includes_all_registered_stocks_but_not_index(temp_db):
    from modules import screener
    from modules.database import get_connection, init_database, market_context

    with get_connection() as conn:
        conn.execute("INSERT INTO stock_basic(ts_code,name,market) VALUES ('AAPL.US','Apple','美股')")
    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            _cn_pool(conn)
        codes = [row["ts_code"] for row in screener.get_all_stocks()]
    assert codes == ["000001.SZ", "600519.SH", "688981.SH"]
    assert [row["ts_code"] for row in screener.get_all_stocks()] == ["AAPL.US"]


def test_cn_klines_keep_stored_real_amount(temp_db):
    from modules import screener
    from modules.database import get_connection, init_database, market_context
    from tests.conftest import write_klines_to_db

    rows = generate_uptrend_klines(n=5, ts_code="600519.SH")
    for row in rows:
        row["amount"] = 123456789.0  # 前复权close×vol不是真实成交额
    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            write_klines_to_db(conn, rows)
        bars = screener.get_recent_klines("600519.SH", 5)
    assert [bar.amount for bar in bars] == [123456789.0] * 5


def test_cn_market_status_uses_hs300_and_same_day_pool(temp_db):
    from modules import screener
    from modules.data_sync import register_cn_assets
    from modules.database import get_connection, init_database, market_context
    from tests.conftest import write_klines_to_db

    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            _cn_pool(conn)
            register_cn_assets(conn, [{"ts_code": "601318.SH", "name": "中国平安", "asset_type": "stock"}])
            write_klines_to_db(conn, generate_uptrend_klines(n=25, ts_code="000300.SH", start_price=4000))
            write_klines_to_db(conn, generate_uptrend_klines(n=25, ts_code="600519.SH"))
            write_klines_to_db(conn, generate_uptrend_klines(n=25, ts_code="000001.SZ"))
            write_klines_to_db(conn, generate_downtrend_klines(n=25, ts_code="688981.SH"))
            # 缺少参照交易日行情的标的不能用旧日期凑数。
            write_klines_to_db(conn, generate_uptrend_klines(n=24, ts_code="601318.SH"))
        status = screener.get_market_status()
    assert status.trade_date == "20250625"
    assert status.market_direction == "LONG"
    text = " ".join(status.reasons)
    assert "沪深300" in text and "20日均线上方" in text
    assert "当前股票池" in text and "2/3" in text and "不代表全A股" in text


def test_cn_market_status_without_index_is_explicit(temp_db):
    from modules import screener
    from modules.database import get_connection, init_database, market_context

    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            _cn_pool(conn)
        status = screener.get_market_status()
    assert status.market_direction == "NEUTRAL" and status.trade_date == ""
    assert any("沪深300无行情" in reason for reason in status.reasons)


class TestStockScore:
    def test_rating_excellent(self):
        s = StockScore(ts_code="600519.SH", score=85)
        assert "★" in s.rating

    def test_rating_poor(self):
        s = StockScore(ts_code="600519.SH", score=10)
        assert "★" in s.rating


class TestMarketStatus:
    def test_defaults(self):
        ms = MarketStatus(trade_date="20260428")
        assert ms.is_trading is True
        assert ms.market_direction == "NEUTRAL"


class TestCalculateMA:
    def test_basic(self):
        assert calculate_ma([1, 2, 3, 4, 5], 5) == 3.0

    def test_insufficient(self):
        assert calculate_ma([1], 5) == 0


class TestCalculateVolMA:
    def test_basic(self):
        assert calculate_vol_ma([100, 200, 300, 400, 500], 5) == 300.0

    def test_insufficient(self):
        assert calculate_vol_ma([100], 5) == 0


class TestCalculateKDJ:
    def test_returns_tuple(self):
        klines = generate_uptrend_klines(n=20)
        k, d, j = calculate_kdj(klines)
        assert isinstance(k, float)

    def test_insufficient_data(self):
        klines = generate_uptrend_klines(n=5)
        k, d, j = calculate_kdj(klines)
        assert (k, d, j) == (50, 50, 50)


class TestCalculateBBI:
    def test_basic(self):
        klines = generate_uptrend_klines(n=30)
        bbi = calculate_bbi(klines)
        assert bbi > 0

    def test_insufficient_data(self):
        klines = generate_uptrend_klines(n=10)
        assert calculate_bbi(klines) == 0


class TestIsPerfectPattern:
    def test_uptrend_perfect(self):
        klines = generate_uptrend_klines(n=50)
        is_perfect, reasons = is_perfect_pattern(klines)
        assert isinstance(is_perfect, bool)
        assert isinstance(reasons, list)

    def test_insufficient_data(self):
        klines = generate_uptrend_klines(n=10)
        is_perfect, reasons = is_perfect_pattern(klines)
        assert is_perfect is False
        assert "数据不足" in reasons


class TestScoreB1Opportunity:
    def test_uptrend_low_score(self):
        klines = generate_uptrend_klines(n=50)
        score, reasons = score_b1_opportunity(klines)
        assert 0 <= score <= 100

    def test_insufficient_data(self):
        klines = generate_uptrend_klines(n=10)
        score, reasons = score_b1_opportunity(klines)
        assert score == 0
        assert "数据不足" in reasons


class TestScoreTrend:
    def test_uptrend(self):
        klines = generate_uptrend_klines(n=50, daily_pct=1.0)
        score, direction = score_trend(klines)
        assert 0 <= score <= 100
        assert any(direction.startswith(d) for d in ("上升", "下降", "震荡"))

    def test_downtrend(self):
        klines = generate_downtrend_klines(n=50)
        score, direction = score_trend(klines)
        assert 0 <= score <= 100

    def test_insufficient_data(self):
        klines = generate_uptrend_klines(n=10)
        score, direction = score_trend(klines)
        assert score == 50
        assert direction == "震荡"


class TestScoreVolumePattern:
    def test_basic(self):
        klines = generate_uptrend_klines(n=20)
        score, reasons = score_volume_pattern(klines)
        assert 0 <= score <= 100

    def test_insufficient_data(self):
        klines = generate_uptrend_klines(n=5)
        score, reasons = score_volume_pattern(klines)
        assert score == 50
        assert "数据不足" in reasons


class TestScoreRisk:
    def test_uptrend_low_risk(self):
        klines = generate_uptrend_klines(n=70)
        score, warnings = score_risk(klines)
        assert 0 <= score <= 100

    def test_insufficient_data(self):
        klines = generate_uptrend_klines(n=10)
        score, warnings = score_risk(klines)
        assert score == 50
        assert "数据不足" in warnings
