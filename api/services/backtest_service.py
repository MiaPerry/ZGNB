"""策略回测服务"""

import logging
import math

from modules.database import cn_snapshot_read, get_connection, get_market

logger = logging.getLogger(__name__)

_MIN_HISTORY = 30
_CN_ASSUMPTIONS = [
    "沿用现有策略模拟引擎，结果仅表示该引擎假设下的策略模拟指标，不代表真实账户收益",
    "进出场价格为前复权参考价，并非历史实际成交价",
    "未模拟T+1、涨跌停成交可行性、板块数量限制、分红及交易费用",
    "按信号日收盘价与整手数量近似成交，净值以初始模拟基值归一化为100",
]
_NOT_IMPLEMENTED = "当前模拟引擎未实现该指标"
_NO_TRADES = "无模拟交易样本，不可计算"
_NO_CURVE = "无有效策略模拟净值，不可计算"
_NO_LOSS = "无亏损交易，盈亏比不可计算"


class BacktestInputError(ValueError):
    """模拟必要输入不满足，由路由映射为 422。"""


def _require_history(ts_codes: list[str]) -> None:
    """CN 行情不足时拒绝模拟，不返回虚假成功与全零收益。"""
    if get_market() != "CN":
        return
    with get_connection() as conn:
        short = []
        for code in ts_codes:
            count = conn.execute("SELECT COUNT(*) FROM daily_kline WHERE ts_code = ?", (code,)).fetchone()[0]
            if count < _MIN_HISTORY:
                short.append(f"{code}({count}条)")
    if short:
        raise BacktestInputError(f"行情不足{_MIN_HISTORY}个交易日，无法模拟：{'、'.join(short)}")


@cn_snapshot_read
def run_shaofu(ts_code: str, days: int = 250, **kwargs) -> dict:
    """少妇战法单股回测"""
    _require_history([ts_code])
    try:
        from modules.backtest_six_step import backtest_shaofu_single

        result = backtest_shaofu_single(ts_code, days=days)
        return _shaofu_to_response(ts_code, result)
    except ImportError:
        # 如果没有 backtest_six_step，回退到基础回测
        logger.warning("backtest_six_step 不可用，使用基础回测")
        return run_multi(ts_code, days=days, **kwargs)


@cn_snapshot_read
def run_multi(
    ts_code: str,
    days: int = 240,
    initial_capital: float = 100000,
    position_pct: float = 0.3,
    stop_loss_pct: float = 0.07,
    take_profit_pct: float = 0.15,
) -> dict:
    """多策略融合回测"""
    from modules.backtest import backtest_multi_strategy

    _require_history([ts_code])
    result = backtest_multi_strategy(
        ts_code,
        days=days,
        initial_capital=initial_capital,
        position_pct=position_pct,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )
    return _portfolio_to_response(ts_code, result)


@cn_snapshot_read
def run_portfolio(
    ts_codes: list[str],
    days: int = 240,
    initial_capital: float = 100000,
    position_pct: float = 0.2,
    stop_loss_pct: float = 0.07,
    take_profit_pct: float = 0.15,
) -> dict:
    """多股票组合回测"""
    from modules.backtest import backtest_portfolio

    _require_history(ts_codes)
    stock_configs = [{"ts_code": code} for code in ts_codes]
    result = backtest_portfolio(
        stock_configs,
        days=days,
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
            row = conn.execute("SELECT MAX(trade_date) FROM daily_kline WHERE ts_code = ?", (ts_code,)).fetchone()
        return {"data_version": get_data_version(ts_code), "data_date": row[0] if row else None}
    except Exception:
        return {"data_version": "", "data_date": None}


def _market_meta() -> dict:
    """结果元数据：CN 固定为前复权策略模拟；US 仅为兼容字段。"""
    if get_market() == "CN":
        return {
            "workspace_market": "CN",
            "simulation_mode": "strategy_simulation",
            "price_basis": "qfq",
            "assumptions": list(_CN_ASSUMPTIONS),
            "metric_unavailable_reasons": {},
        }
    return {
        "workspace_market": get_market(),
        "simulation_mode": "backtest",
        "price_basis": "source",
        "assumptions": [],
        "metric_unavailable_reasons": {},
    }


def _apply_cn_contract(resp: dict, unavailable: dict[str, str]) -> dict:
    """CN 不可计算指标返回 null 与字段级原因；有公式支持的真实 0 保留。"""
    summary, reasons = resp["summary"], resp["metric_unavailable_reasons"]
    for key, reason in unavailable.items():
        summary[key] = None
        reasons[key] = reason
    for key, value in summary.items():
        if isinstance(value, float) and not math.isfinite(value):
            summary[key] = None
            reasons[key] = _NO_LOSS if key == "profit_factor" else "计算结果非有限值，不可计算"
    if not resp["equity_curve"]:
        reasons["equity_curve"] = "无有效模拟交易，无法生成策略模拟净值"
    return resp


def _cn_shaofu_unavailable(result) -> dict[str, str]:
    unavailable = {"annualized_return": _NOT_IMPLEMENTED}
    if result.total_trades == 0:
        unavailable.update(
            dict.fromkeys(
                (
                    "win_rate",
                    "profit_factor",
                    "total_return",
                    "max_drawdown",
                    "sharpe_ratio",
                    "avg_holding_days",
                    "avg_pnl",
                    "max_win",
                    "max_loss",
                ),
                _NO_TRADES,
            )
        )
    elif result.total_trades < 2:
        unavailable["sharpe_ratio"] = "模拟交易少于2笔，夏普比率不可计算"
    return unavailable


def _cn_portfolio_unavailable(result) -> dict[str, str]:
    unavailable = dict.fromkeys(("avg_holding_days", "win_count", "max_win", "max_loss"), _NOT_IMPLEMENTED)
    if not hasattr(result, "avg_return"):
        unavailable["avg_pnl"] = _NOT_IMPLEMENTED
    if not getattr(result, "equity_curve", []):
        unavailable.update(
            dict.fromkeys(("total_return", "max_drawdown", "annualized_return", "sharpe_ratio"), _NO_CURVE)
        )
    if result.total_trades == 0:
        unavailable.update(dict.fromkeys(("win_rate", "profit_factor", "sharpe_ratio"), _NO_TRADES))
    return unavailable


def _shaofu_to_response(ts_code: str, result) -> dict:
    """将少妇战法结果转为 API 响应"""
    trades = []
    for t in result.trades:
        trades.append(
            {
                "entry_date": t.entry_date,
                "entry_price": round(t.entry_price, 2),
                "exit_date": t.exit_date,
                "exit_price": round(t.exit_price, 2) if t.exit_price else None,
                "exit_reason": t.exit_reason,
                "pnl_pct": round(t.pnl_pct * 100, 2),
                "holding_days": t.holding_days,
            }
        )

    # 构建资金曲线（从 trades 推算）；CN 为以100为基值的策略模拟净值
    is_cn = get_market() == "CN"
    equity_curve = _build_equity_curve_from_trades(
        result.trades, 100.0 if is_cn else getattr(result, "initial_capital", 100000)
    )

    resp = {
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
        **_market_meta(),
    }
    return _apply_cn_contract(resp, _cn_shaofu_unavailable(result)) if is_cn else resp


def _portfolio_to_response(ts_code: str, result) -> dict:
    """将组合回测结果转为 API 响应"""
    trades = []
    for t in result.trades:
        trades.append(
            {
                "ts_code": getattr(t, "ts_code", ts_code),
                "entry_date": t.entry_date,
                "entry_price": round(t.entry_price, 2),
                "exit_date": t.exit_date,
                "exit_price": round(t.exit_price, 2) if t.exit_price else None,
                "exit_reason": t.exit_reason,
                "pnl_pct": round(t.pnl_pct * 100, 2),
                "holding_days": getattr(t, "holding_days", 0),
            }
        )

    # 资金曲线；CN 按初始模拟基值归一化为100，不展示人民币资产余额
    is_cn = get_market() == "CN"
    base = getattr(result, "initial_capital", 0) or 0
    equity_curve = []
    for date, value in getattr(result, "equity_curve", []):
        if is_cn:
            if base <= 0:
                equity_curve = []
                break
            value = value / base * 100
        equity_curve.append([date, round(value, 2)])

    resp = {
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
        **_market_meta(),
    }
    return _apply_cn_contract(resp, _cn_portfolio_unavailable(result)) if is_cn else resp


def _build_equity_curve_from_trades(trades, initial_capital: float) -> list:
    """从交易列表推算简易资金曲线"""
    if not trades:
        return []

    capital = initial_capital
    curve = []
    for t in trades:
        pnl = getattr(t, "pnl_pct", 0)
        capital *= 1 + pnl
        exit_date = getattr(t, "exit_date", None) or getattr(t, "entry_date", "")
        curve.append([exit_date, round(capital, 2)])

    return curve
