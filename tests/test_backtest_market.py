"""回测市场隔离：CN 策略模拟元数据、不可计算契约与数据不足拒绝；US 数值保持原样"""

import math
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

SHAOFU = "modules.backtest_six_step.backtest_shaofu_single"
MULTI = "modules.backtest.backtest_multi_strategy"
PORTFOLIO = "modules.backtest.backtest_portfolio"
CN = {"X-Market": "CN"}


def _trade(exit_date, pnl_pct):
    return SimpleNamespace(
        entry_date="20260101",
        entry_price=10.0,
        exit_date=exit_date,
        exit_price=11.0,
        exit_reason="signal",
        pnl_pct=pnl_pct,
        pnl=pnl_pct * 1000,
        holding_days=3,
    )


def _shaofu_result(trades, **overrides):
    values = dict(
        trades=trades,
        total_trades=len(trades),
        win_rate=1.0,
        profit_factor=math.inf,
        total_return=0.155,
        max_drawdown=0.0,
        sharpe_ratio=5.0,
        avg_holding_days=3,
        win_count=len(trades),
        avg_pnl=0.075,
        max_win=0.1,
        max_loss=0.05,
        initial_capital=100000,
    )
    if not trades:
        values.update(
            win_rate=0,
            profit_factor=0,
            total_return=0,
            sharpe_ratio=0,
            avg_holding_days=0,
            avg_pnl=0,
            max_win=0,
            max_loss=0,
        )
    values.update(overrides)
    return SimpleNamespace(**values)


def _multi_result(trades, curve, **overrides):
    values = dict(
        initial_capital=100000.0,
        equity_curve=curve,
        trades=trades,
        total_trades=len(trades),
        win_rate=1.0 if trades else 0,
        profit_factor=math.inf if trades else 0,
        total_return=0.05 if trades else 0,
        max_drawdown=0.0,
        sharpe_ratio=3.0 if trades else 0,
        annualized_return=0.2 if trades else 0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def cn_db(temp_db):
    from modules.database import get_connection, init_database, market_context

    def seed(code, rows):
        with market_context("CN"), get_connection() as conn:
            conn.executemany(
                "INSERT INTO daily_kline(ts_code,trade_date,open,high,low,close,vol,amount,pct_chg) "
                "VALUES (?,?,10,10,10,10,100,1000,0)",
                [(code, f"2026{i // 28 + 1:02d}{i % 28 + 1:02d}") for i in range(rows)],
            )

    with market_context("CN"):
        init_database()
    return seed


def _run_cn(entry, *args):
    from api.services import backtest_service
    from modules.database import market_context

    with market_context("CN"):
        return getattr(backtest_service, entry)(*args)


def test_us_shaofu_adds_compat_metadata_without_changing_values(db_conn, monkeypatch):
    from api.services import backtest_service

    monkeypatch.setattr(
        SHAOFU, lambda *a, **k: _shaofu_result([_trade("20260110", 0.1), _trade("20260120", 0.05)], profit_factor=2.0)
    )
    resp = backtest_service.run_shaofu("ADI.US")
    assert resp["summary"]["annualized_return"] == 0
    assert resp["summary"]["profit_factor"] == 2.0
    assert resp["equity_curve"] == [["20260110", 110000.0], ["20260120", 115500.0]]
    assert resp["workspace_market"] == "US"
    assert resp["simulation_mode"] == "backtest"
    assert resp["price_basis"] == "source"
    assert resp["assumptions"] == [] and resp["metric_unavailable_reasons"] == {}


def test_cn_shaofu_marks_simulation_and_nulls_uncomputable_metrics(cn_db, monkeypatch):
    cn_db("600519.SH", 40)
    monkeypatch.setattr(SHAOFU, lambda *a, **k: _shaofu_result([_trade("20260110", 0.1), _trade("20260120", 0.05)]))
    resp = _run_cn("run_shaofu", "600519.SH")
    summary, reasons = resp["summary"], resp["metric_unavailable_reasons"]
    assert resp["workspace_market"] == "CN"
    assert resp["simulation_mode"] == "strategy_simulation"
    assert resp["price_basis"] == "qfq"
    assert resp["assumptions"]
    assert summary["profit_factor"] is None and "亏损" in reasons["profit_factor"]
    assert summary["annualized_return"] is None and reasons["annualized_return"]
    assert summary["win_rate"] == 1.0 and summary["total_return"] == 15.5
    assert summary["max_drawdown"] == 0.0 and "max_drawdown" not in reasons
    assert resp["equity_curve"] == [["20260110", 110.0], ["20260120", 115.5]]


def test_cn_shaofu_without_trades_keeps_real_count_and_nulls_ratios(cn_db, monkeypatch):
    cn_db("600519.SH", 40)
    monkeypatch.setattr(SHAOFU, lambda *a, **k: _shaofu_result([]))
    resp = _run_cn("run_shaofu", "600519.SH")
    summary, reasons = resp["summary"], resp["metric_unavailable_reasons"]
    assert summary["total_trades"] == 0 and summary["win_count"] == 0
    for key in (
        "win_rate",
        "profit_factor",
        "total_return",
        "max_drawdown",
        "sharpe_ratio",
        "avg_holding_days",
        "avg_pnl",
        "max_win",
        "max_loss",
    ):
        assert summary[key] is None and reasons[key]
    assert resp["equity_curve"] == [] and reasons["equity_curve"]


def test_cn_multi_normalizes_curve_and_drops_placeholder_zeros(cn_db, monkeypatch):
    cn_db("600519.SH", 40)
    monkeypatch.setattr(
        MULTI,
        lambda *a, **k: _multi_result([_trade("20260120", 0.05)], [("20260110", 100000.0), ("20260120", 105000.0)]),
    )
    resp = _run_cn("run_multi", "600519.SH")
    summary, reasons = resp["summary"], resp["metric_unavailable_reasons"]
    assert resp["equity_curve"] == [["20260110", 100.0], ["20260120", 105.0]]
    for key in ("avg_holding_days", "win_count", "avg_pnl", "max_win", "max_loss"):
        assert summary[key] is None and "未实现" in reasons[key]
    assert summary["annualized_return"] == 20.0 and summary["profit_factor"] is None


def test_cn_multi_flat_curve_keeps_real_zero_returns(cn_db, monkeypatch):
    cn_db("600519.SH", 40)
    monkeypatch.setattr(MULTI, lambda *a, **k: _multi_result([], [("20260110", 100000.0), ("20260120", 100000.0)]))
    resp = _run_cn("run_multi", "600519.SH")
    summary, reasons = resp["summary"], resp["metric_unavailable_reasons"]
    assert summary["total_return"] == 0.0 and summary["max_drawdown"] == 0.0
    assert summary["win_rate"] is None and summary["sharpe_ratio"] is None and reasons["sharpe_ratio"]
    assert resp["equity_curve"] == [["20260110", 100.0], ["20260120", 100.0]]


@pytest.mark.parametrize(
    "path,body,engine",
    [
        ("/api/v1/backtest/shaofu", {"ts_code": "600519.SH"}, SHAOFU),
        ("/api/v1/backtest/multi", {"ts_code": "600519.SH"}, MULTI),
        ("/api/v1/backtest/portfolio", {"ts_codes": ["000001.SZ", "600519.SH"]}, PORTFOLIO),
    ],
)
def test_cn_insufficient_history_rejected_before_engine(cn_db, monkeypatch, path, body, engine):
    from api.main import app

    cn_db("000001.SZ", 40)
    cn_db("600519.SH", 10)

    def engine_called(*a, **k):
        raise AssertionError("数据不足时不应运行模拟")

    monkeypatch.setattr(engine, engine_called)
    with TestClient(app) as client:
        response = client.post(path, headers=CN, json=body)
    assert response.status_code == 422
    assert "600519.SH" in response.json()["detail"] and "不足" in response.json()["detail"]


def test_cn_api_serializes_null_metrics_and_metadata(cn_db, monkeypatch):
    from api.main import app

    cn_db("600519.SH", 40)
    monkeypatch.setattr(
        MULTI,
        lambda *a, **k: _multi_result([_trade("20260120", 0.05)], [("20260110", 100000.0), ("20260120", 105000.0)]),
    )
    with TestClient(app) as client:
        data = client.post("/api/v1/backtest/multi", headers=CN, json={"ts_code": "600519.SH"}).json()
    assert data["summary"]["avg_holding_days"] is None and data["summary"]["profit_factor"] is None
    assert data["simulation_mode"] == "strategy_simulation" and data["price_basis"] == "qfq"
    assert data["workspace_market"] == "CN" and data["metric_unavailable_reasons"]["win_count"]
    assert data["equity_curve"][-1] == ["20260120", 105.0]
