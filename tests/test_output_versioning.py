"""输出结果与数据版本绑定：AI 点评缓存键与回测元数据。"""


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
        "ts_code": "ADI.US", "trade_date": "20260917", "name": "", "price": 1,
        "pct_chg": 0, "indicators": {}, "score": {}, "diagnosis": {}, "signals": [],
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


def test_backtest_response_binds_data_version(db_conn, monkeypatch):
    from types import SimpleNamespace

    from api.services import backtest_service
    from tests.conftest import write_klines_to_db

    write_klines_to_db(db_conn, [{
        "ts_code": "ADI.US", "trade_date": "20260917", "open": 1, "high": 1,
        "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0,
    }])
    fake_result = SimpleNamespace(
        trades=[], total_trades=0, win_rate=0, profit_factor=0, total_return=0,
        max_drawdown=0, sharpe_ratio=0, avg_holding_days=0, win_count=0,
        avg_pnl=0, max_win=0, max_loss=0, initial_capital=100000,
    )
    monkeypatch.setattr(
        "modules.backtest_six_step.backtest_shaofu_single", lambda *a, **k: fake_result
    )

    resp = backtest_service.run_shaofu("ADI.US", days=60)
    assert resp["data_date"] == "20260917"
    assert resp["data_version"]

    # 组合回测使用全局版本，不能退化为永不变化的 per-stock 版本
    portfolio = backtest_service._portfolio_to_response("portfolio", SimpleNamespace(
        trades=[], total_trades=0, win_rate=0, profit_factor=0, total_return=0,
        max_drawdown=0, sharpe_ratio=0, avg_return=0, annualized_return=0,
        equity_curve=[],
    ))
    assert portfolio["data_version"]
    assert portfolio["data_date"] is None
