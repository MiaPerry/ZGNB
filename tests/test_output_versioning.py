"""输出结果与数据版本绑定：AI 点评缓存键与回测元数据。"""

import pytest


def test_cn_read_snapshot_keeps_bars_indicators_and_version_atomic(temp_db):
    from concurrent.futures import ThreadPoolExecutor
    from modules import database, data_freshness
    from modules.indicators.data_layer import get_kline_data

    code = "600519.SH"
    with database.market_context("CN") as binding:
        database.init_database()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO daily_kline(ts_code,trade_date,close) VALUES (?, '20261020', 10)", (code,))
            conn.execute("INSERT INTO indicator_cache(ts_code,trade_date,close) VALUES (?, '20261020', 10)", (code,))
            data_freshness.record_data_change(conn, code, "20261020")
        old_version = data_freshness.get_data_version(code)

        def publish():
            with database.market_context("CN", binding.db_path), database.get_connection() as conn:
                conn.execute("UPDATE daily_kline SET close=20")
                conn.execute("UPDATE indicator_cache SET close=20")
                data_freshness.record_data_change(conn, code, "20261020")

        with database.read_snapshot():
            assert get_kline_data(code)[-1].close == 10
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(publish).result(timeout=5)
            assert get_kline_data(code)[-1].close == 10
            assert data_freshness.get_data_version(code) == old_version
            with database.get_connection() as conn:
                assert conn.execute("SELECT close FROM indicator_cache").fetchone()[0] == 10
            with database.read_snapshot(), database.get_connection() as conn:
                assert conn.execute("SELECT close FROM daily_kline").fetchone()[0] == 10
                with pytest.raises(Exception, match="readonly"):
                    conn.execute("UPDATE daily_kline SET close=30")
        assert get_kline_data(code)[-1].close == 20
        assert data_freshness.get_data_version(code) != old_version


@pytest.mark.parametrize(
    "entry,dependency",
    [
        ("get_kline_chart_data", "modules.indicators.data_layer.get_kline_data"),
        ("get_signals", "modules.strategies.detect_all_strategies"),
        ("get_score", "modules.screener.analyze_stock"),
        ("diagnose", "modules.portfolio_diagnosis.diagnose_stock"),
    ],
)
def test_cn_independent_read_entries_freeze_input(temp_db, monkeypatch, entry, dependency):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from api.services import stock_service, diagnosis_service
    from modules import database, data_freshness

    code = "600519.SH"
    with database.market_context("CN") as binding:
        database.init_database()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO daily_kline(ts_code,trade_date,close) VALUES (?, '20261020', 10)", (code,))
            data_freshness.record_data_change(conn, code, "20261020")
        version = data_freshness.get_data_version(code)

        def read_then_publish(*args, **kwargs):
            with database.get_connection() as conn:
                assert conn.execute("SELECT close FROM daily_kline").fetchone()[0] == 10

            def publish():
                with database.market_context("CN", binding.db_path), database.get_connection() as conn:
                    conn.execute("UPDATE daily_kline SET close=20")
                    data_freshness.record_data_change(conn, code, "20261020")

            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(publish).result(timeout=5)
            with database.get_connection() as conn:
                assert conn.execute("SELECT close FROM daily_kline").fetchone()[0] == 10
            assert data_freshness.get_data_version(code) == version
            if entry == "get_score":
                from modules.screener import StockScore

                return StockScore(ts_code=code)
            return [] if entry in {"get_kline_chart_data", "get_signals"} else SimpleNamespace()

        monkeypatch.setattr(dependency, read_then_publish)
        service = diagnosis_service if entry == "diagnose" else stock_service
        getattr(service, entry)(code)


@pytest.mark.parametrize(
    "entry,dependency,result",
    [
        ("scan_watchlist", "modules.watchlist.scan_watchlist", {"alerts": [], "summary": {}}),
        ("generate_report", "modules.watchlist.generate_daily_report", ""),
    ],
)
def test_cn_watchlist_scan_reads_one_snapshot(temp_db, monkeypatch, entry, dependency, result):
    from api.services import watchlist_service
    from modules import database

    seen = []

    def scan(*args, **kwargs):
        seen.append(database.get_read_snapshot_connection() is not None)
        return result

    monkeypatch.setattr(dependency, scan)
    with database.market_context("CN"):
        database.init_database()
        getattr(watchlist_service, entry)()
    getattr(watchlist_service, entry)()
    assert seen == [True, False]


def test_cn_snapshot_freezes_legacy_epoch_without_writing(temp_db, monkeypatch):
    from pathlib import Path
    from modules import database, data_freshness

    with database.market_context("CN"):
        database.init_database()
        real_stat = Path.stat

        class ChangedStat:
            st_ctime_ns = 1

            def __init__(self, stat):
                self.stat = stat

            def __getattr__(self, name):
                return getattr(self.stat, name)

        with database.read_snapshot():
            version = data_freshness.get_data_version()
            monkeypatch.setattr(Path, "stat", lambda path, *a, **kw: ChangedStat(real_stat(path, *a, **kw)))
            assert data_freshness.get_data_version() == version
            with database.get_connection() as conn:
                assert conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0


def test_analysis_api_retains_frozen_market_version_not_local_path(temp_db, monkeypatch):
    from fastapi.testclient import TestClient
    from api.main import app
    from api.services import stock_service
    from modules import database

    database.init_database("CN")
    monkeypatch.setattr(
        stock_service,
        "get_full_analysis",
        lambda *a, **kw: {
            "ts_code": "600519.SH",
            "workspace_market": "CN",
            "data_version": "frozen:1",
            "days": 120,
            "database_identity": "internal/local/path.db",
        },
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/stock/analyze/600519.SH", headers={"X-Market": "CN"})
    assert response.status_code == 200
    assert response.json()["workspace_market"] == "CN"
    assert response.json()["data_version"] == "frozen:1"
    assert response.json()["days"] == 120
    assert "database_identity" not in response.json()


def test_cn_full_analysis_freezes_all_input_reads(temp_db, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from api.services import stock_service
    from modules import database, data_freshness, data_sync, indicators
    from tests.test_data_sync_extensions import _cn_daily_source

    code = "600519.SH"
    daily, factors = _cn_daily_source(code)
    with database.market_context("CN") as binding:
        database.init_database()
        with database.get_connection() as conn:
            data_sync.register_cn_assets(
                conn, [{"ts_code": code, "name": "测试股", "asset_type": "stock", "sources": ["manual"]}]
            )
        data_sync.publish_cn_daily_candidate(
            data_sync.build_cn_daily_candidate(code, daily, factors), data_freshness.get_data_version(code)
        )
        old_version = data_freshness.get_data_version(code)
        real_analyze = indicators.analyze_stock

        def analyze_then_publish(*args, **kwargs):
            result = real_analyze(*args, **kwargs)

            def publish():
                with database.market_context("CN", binding.db_path), database.get_connection() as conn:
                    conn.execute("UPDATE daily_kline SET open=open*2,high=high*2,low=low*2,close=close*2")
                    conn.execute("UPDATE indicator_cache SET close=close*2")
                    data_freshness.record_data_change(conn, code, "20261020")

            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(publish).result(timeout=5)
            return result

        monkeypatch.setattr(indicators, "analyze_stock", analyze_then_publish)
        result = stock_service.get_full_analysis(code)
        assert result["price"] == 55 and result["prev_close"] == 52.5
        assert result["data_version"] == old_version and result["workspace_market"] == "CN"


def test_commentary_cache_key_tracks_data_version(db_conn, monkeypatch):
    from modules import commentary_service
    from modules.data_freshness import record_data_change

    commentary_service._cache.clear()
    calls = []

    class FakeProvider:
        def generate(self, system_prompt, user_prompt, temperature=0.7):
            calls.append(user_prompt)
            return "Z哥点评文本"

    monkeypatch.setattr("modules.llm_providers.MiniMaxProvider", lambda: FakeProvider())

    analysis = {
        "ts_code": "ADI.US",
        "trade_date": "20260917",
        "name": "",
        "price": 1,
        "pct_chg": 0,
        "indicators": {},
        "score": {},
        "diagnosis": {},
        "signals": [],
    }

    first = commentary_service.generate_commentary(analysis)
    assert len(calls) == 1 and first["data_version"]

    second = commentary_service.generate_commentary(analysis)
    assert len(calls) == 1, "同一数据版本应命中缓存，不重复调用 LLM"
    assert second["cached"] is True and second["data_version"] == first["data_version"]

    # 数据版本变化后（如同日补齐指标），手动生成不得命中旧输入的点评
    record_data_change(db_conn, "ADI.US", "20260917")
    db_conn.commit()
    third = commentary_service.generate_commentary(analysis)
    assert len(calls) == 2 and third["cached"] is False
    assert third["data_version"] != first["data_version"]
    commentary_service._cache.clear()


@pytest.mark.parametrize("dimension", ["market", "path", "window"])
def test_commentary_cache_isolates_identity_and_window(db_conn, tmp_path, monkeypatch, dimension):
    from modules import commentary_service, data_freshness
    from modules.database import init_database, market_context

    commentary_service._cache.clear()
    monkeypatch.setattr(data_freshness, "get_data_version", lambda code: "copied-epoch:1")
    calls = []

    class FakeProvider:
        def generate(self, system_prompt, user_prompt, temperature=0.7):
            calls.append(user_prompt)
            return f"点评{len(calls)}"

    monkeypatch.setattr("modules.llm_providers.MiniMaxProvider", lambda: FakeProvider())
    analysis = {"ts_code": "600519.SH", "trade_date": "20260917", "days": 120, "price": 1}
    try:
        first = commentary_service.generate_commentary(analysis)
        if dimension == "window":
            second = commentary_service.generate_commentary({**analysis, "days": 60})
        else:
            with market_context("CN" if dimension == "market" else "US", tmp_path / "other.db"):
                init_database()
                second = commentary_service.generate_commentary(analysis)
        assert len(calls) == 2
        assert not second["cached"] and first["commentary_text"] != second["commentary_text"]
        assert commentary_service.generate_commentary(analysis)["cached"]
    finally:
        commentary_service._cache.clear()


@pytest.mark.parametrize(
    "market,code,asset_type,unit",
    [
        ("US", "ADI.US", "stock", "USD"),
        ("US", "00700.HK", "stock", "HKD"),
        ("CN", "600519.SH", "stock", "CNY"),
        ("CN", "000300.SH", "index", "点"),
    ],
)
def test_commentary_prompt_has_market_currency_and_rule_boundaries(temp_db, market, code, asset_type, unit):
    from modules import commentary_service
    from modules.database import market_context

    with market_context(market):
        prompt = commentary_service._build_user_prompt({"ts_code": code, "asset_type": asset_type, "price": 12})
    assert market in prompt and unit in prompt
    assert "¥" not in prompt
    if market == "CN":
        assert "前复权" in prompt if asset_type == "stock" else "不适用" in prompt
        assert "涨跌停" in prompt


def test_commentary_uses_frozen_input_version_and_window(db_conn, monkeypatch):
    from modules import commentary_service

    commentary_service._cache.clear()
    analysis = {
        "ts_code": "ADI.US",
        "trade_date": "20260917",
        "price": 11,
        "data_version": "frozen:7",
        "indicators": {"kdj": {"k": 25}},
    }
    prompts = []

    def mutate_original():
        analysis.update(price=99, trade_date="20260918")
        analysis["indicators"]["kdj"]["k"] = 90
        return ""

    class FakeProvider:
        def generate(self, system_prompt, user_prompt, temperature=0.7):
            prompts.append(user_prompt)
            return "冻结输入的点评"

    monkeypatch.setattr(commentary_service, "_load_skill_sections", mutate_original)
    monkeypatch.setattr("modules.llm_providers.MiniMaxProvider", lambda: FakeProvider())
    try:
        result = commentary_service.generate_commentary(analysis, days=60)
        assert result["data_version"] == "frozen:7"
        assert result["trade_date"] == "20260917"
        assert "K=25.00" in prompts[0] and "K=90.00" not in prompts[0]
        assert "60" in prompts[0] and "20260918" not in prompts[0]
    finally:
        commentary_service._cache.clear()


def test_commentary_api_forwards_requested_window(db_conn, monkeypatch):
    from fastapi.testclient import TestClient
    from api.main import app
    from api.routes import commentary
    from modules import commentary_service

    analysis = {"ts_code": "ADI.US", "trade_date": "20260917", "price": 11}
    monkeypatch.setattr(commentary.stock_service, "get_full_analysis", lambda code, days: dict(analysis))
    calls = []

    class FakeProvider:
        def generate(self, system_prompt, user_prompt, temperature=0.7):
            calls.append(user_prompt)
            return "测试点评"

    monkeypatch.setattr("modules.llm_providers.MiniMaxProvider", lambda: FakeProvider())
    commentary_service._cache.clear()
    try:
        with TestClient(app) as client:
            for days in [60, 120, 60]:
                assert client.post(f"/api/v1/commentary/ADI.US?days={days}").status_code == 200
        assert len(calls) == 2
        assert "窗口 60" in calls[0] and "窗口 120" in calls[1]
    finally:
        commentary_service._cache.clear()


@pytest.mark.parametrize(
    "entry,engine",
    [
        ("run_shaofu", "modules.backtest_six_step.backtest_shaofu_single"),
        ("run_multi", "modules.backtest.backtest_multi_strategy"),
        ("run_portfolio", "modules.backtest.backtest_portfolio"),
    ],
)
def test_cn_backtest_input_and_response_version_share_snapshot(temp_db, monkeypatch, entry, engine):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from api.services import backtest_service
    from modules import database, data_freshness
    from modules.indicators.data_layer import get_kline_data

    code = "600519.SH"
    with database.market_context("CN") as binding:
        database.init_database()
        with database.get_connection() as conn:
            conn.executemany(
                "INSERT INTO daily_kline(ts_code,trade_date,close) VALUES (?, ?, 10)",
                [(code, f"202609{day:02d}") for day in range(1, 30)] + [(code, "20261020")],
            )
            data_freshness.record_data_change(conn, code, "20261020")
        old_version = data_freshness.get_data_version(code if entry != "run_portfolio" else None)

        def engine_call(*args, **kwargs):
            assert get_kline_data(code)[-1].close == 10

            def publish():
                with database.market_context("CN", binding.db_path), database.get_connection() as conn:
                    conn.execute("UPDATE daily_kline SET close=20")
                    data_freshness.record_data_change(conn, code, "20261020")

            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(publish).result(timeout=5)
            assert get_kline_data(code)[-1].close == 10
            return SimpleNamespace(
                trades=[],
                total_trades=0,
                win_rate=0,
                profit_factor=0,
                total_return=0,
                max_drawdown=0,
                sharpe_ratio=0,
                avg_holding_days=0,
                win_count=0,
                avg_pnl=0,
                max_win=0,
                max_loss=0,
                initial_capital=100000,
                equity_curve=[],
            )

        monkeypatch.setattr(engine, engine_call)
        response = getattr(backtest_service, entry)([code] if entry == "run_portfolio" else code)
        assert response["data_version"] == old_version


def test_backtest_response_binds_data_version(db_conn, monkeypatch):
    from types import SimpleNamespace

    from api.services import backtest_service
    from tests.conftest import write_klines_to_db

    write_klines_to_db(
        db_conn,
        [
            {
                "ts_code": "ADI.US",
                "trade_date": "20260917",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "vol": 1,
                "amount": 1,
                "pct_chg": 0,
            }
        ],
    )
    fake_result = SimpleNamespace(
        trades=[],
        total_trades=0,
        win_rate=0,
        profit_factor=0,
        total_return=0,
        max_drawdown=0,
        sharpe_ratio=0,
        avg_holding_days=0,
        win_count=0,
        avg_pnl=0,
        max_win=0,
        max_loss=0,
        initial_capital=100000,
    )
    monkeypatch.setattr("modules.backtest_six_step.backtest_shaofu_single", lambda *a, **k: fake_result)

    resp = backtest_service.run_shaofu("ADI.US", days=60)
    assert resp["data_date"] == "20260917"
    assert resp["data_version"]

    # 组合回测使用全局版本，不能退化为永不变化的 per-stock 版本
    portfolio = backtest_service._portfolio_to_response(
        "portfolio",
        SimpleNamespace(
            trades=[],
            total_trades=0,
            win_rate=0,
            profit_factor=0,
            total_return=0,
            max_drawdown=0,
            sharpe_ratio=0,
            avg_return=0,
            annualized_return=0,
            equity_curve=[],
        ),
    )
    assert portfolio["data_version"]
    assert portfolio["data_date"] is None
