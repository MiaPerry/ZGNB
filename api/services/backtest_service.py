"""策略回测服务"""

import logging

logger = logging.getLogger(__name__)


def run_shaofu(ts_code: str, days: int = 250, **kwargs) -> dict:
    """少妇战法单股回测"""
    try:
        from modules.backtest_six_step import backtest_shaofu_single
        result = backtest_shaofu_single(ts_code, days=days)
        return _shaofu_to_response(ts_code, result)
    except ImportError:
        # 如果没有 backtest_six_step，回退到基础回测
        logger.warning("backtest_six_step 不可用，使用基础回测")
        return run_multi(ts_code, days=days, **kwargs)


def run_multi(ts_code: str, days: int = 240, initial_capital: float = 100000,
              position_pct: float = 0.3, stop_loss_pct: float = 0.07,
              take_profit_pct: float = 0.15) -> dict:
    """多策略融合回测"""
    from modules.backtest import backtest_multi_strategy

    result = backtest_multi_strategy(
        ts_code, days=days,
        initial_capital=initial_capital,
        position_pct=position_pct,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )
    return _portfolio_to_response(ts_code, result)


def run_portfolio(ts_codes: list[str], days: int = 240, initial_capital: float = 100000,
                  position_pct: float = 0.2, stop_loss_pct: float = 0.07,
                  take_profit_pct: float = 0.15) -> dict:
    """多股票组合回测"""
    from modules.backtest import backtest_portfolio

    stock_configs = [{"ts_code": code} for code in ts_codes]
    result = backtest_portfolio(
        stock_configs, days=days,
        initial_capital=initial_capital,
        position_pct=position_pct,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )
    return _portfolio_to_response("portfolio", result)


# ── 内部序列化 ──

def _data_meta(ts_code: str) -> dict:
    """回测结果绑定的数据版本与实际截止日期；组合回测使用全局版本。"""
    try:
        from modules.data_freshness import get_data_version

        if "." not in ts_code:
            return {"data_version": get_data_version(), "data_date": None}
        from modules.database import get_connection

        with get_connection() as conn:
            row = conn.execute(
                "SELECT MAX(trade_date) FROM daily_kline WHERE ts_code = ?", (ts_code,)
            ).fetchone()
        return {"data_version": get_data_version(ts_code), "data_date": row[0] if row else None}
    except Exception:
        return {"data_version": "", "data_date": None}

def _shaofu_to_response(ts_code: str, result) -> dict:
    """将少妇战法结果转为 API 响应"""
    trades = []
    for t in result.trades:
        trades.append({
            "entry_date": t.entry_date,
            "entry_price": round(t.entry_price, 2),
            "exit_date": t.exit_date,
            "exit_price": round(t.exit_price, 2) if t.exit_price else None,
            "exit_reason": t.exit_reason,
            "pnl_pct": round(t.pnl_pct * 100, 2),
            "holding_days": t.holding_days,
        })

    # 构建资金曲线（从 trades 推算）
    equity_curve = _build_equity_curve_from_trades(
        result.trades, getattr(result, "initial_capital", 100000)
    )

    return {
        "ts_code": ts_code,
        "summary": {
            "total_trades": result.total_trades,
            "win_rate": round(result.win_rate, 3),
            "profit_factor": round(result.profit_factor, 2),
            "total_return": round(result.total_return * 100, 2),
            "max_drawdown": round(result.max_drawdown * 100, 2),
            "sharpe_ratio": round(result.sharpe_ratio, 2),
            "avg_holding_days": round(result.avg_holding_days, 1),
            "annualized_return": 0,
            "win_count": result.win_count,
            "avg_pnl": round(result.avg_pnl * 100, 2),
            "max_win": round(result.max_win * 100, 2),
            "max_loss": round(result.max_loss * 100, 2),
        },
        "equity_curve": equity_curve,
        "trades": trades,
        **_data_meta(ts_code),
    }


def _portfolio_to_response(ts_code: str, result) -> dict:
    """将组合回测结果转为 API 响应"""
    trades = []
    for t in result.trades:
        trades.append({
            "ts_code": getattr(t, "ts_code", ts_code),
            "entry_date": t.entry_date,
            "entry_price": round(t.entry_price, 2),
            "exit_date": t.exit_date,
            "exit_price": round(t.exit_price, 2) if t.exit_price else None,
            "exit_reason": t.exit_reason,
            "pnl_pct": round(t.pnl_pct * 100, 2),
            "holding_days": getattr(t, "holding_days", 0),
        })

    # 资金曲线
    equity_curve = []
    for date, value in getattr(result, "equity_curve", []):
        equity_curve.append([date, round(value, 2)])

    return {
        "ts_code": ts_code,
        "summary": {
            "total_trades": result.total_trades,
            "win_rate": round(result.win_rate, 3),
            "profit_factor": round(result.profit_factor, 2),
            "total_return": round(result.total_return * 100, 2),
            "max_drawdown": round(result.max_drawdown * 100, 2),
            "sharpe_ratio": round(result.sharpe_ratio, 2),
            "avg_holding_days": 0,
            "annualized_return": round(getattr(result, "annualized_return", 0) * 100, 2),
            "win_count": 0,
            "avg_pnl": round(result.avg_return * 100, 2) if hasattr(result, "avg_return") else 0,
            "max_win": 0,
            "max_loss": 0,
        },
        "equity_curve": equity_curve,
        "trades": trades,
        **_data_meta(ts_code),
    }


def _build_equity_curve_from_trades(trades, initial_capital: float) -> list:
    """从交易列表推算简易资金曲线"""
    if not trades:
        return []

    capital = initial_capital
    curve = []
    for t in trades:
        pnl = getattr(t, "pnl_pct", 0)
        capital *= (1 + pnl)
        exit_date = getattr(t, "exit_date", None) or getattr(t, "entry_date", "")
        curve.append([exit_date, round(capital, 2)])

    return curve
