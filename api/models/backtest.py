"""回测模型"""

from pydantic import BaseModel, Field


class BacktestRequest(BaseModel):
    ts_code: str
    days: int = Field(default=250, ge=30, le=1000)
    initial_capital: float = Field(default=100000, ge=10000)
    stop_loss_pct: float = Field(default=0.07, ge=0.01, le=0.5)
    take_profit_pct: float = Field(default=0.15, ge=0.01, le=1.0)
    position_pct: float = Field(default=1.0, ge=0.1, le=1.0)


class MultiBacktestRequest(BaseModel):
    ts_code: str
    days: int = Field(default=240, ge=30, le=1000)
    initial_capital: float = Field(default=100000, ge=10000)
    position_pct: float = Field(default=0.3, ge=0.1, le=1.0)
    stop_loss_pct: float = Field(default=0.07, ge=0.01, le=0.5)
    take_profit_pct: float = Field(default=0.15, ge=0.01, le=1.0)


class PortfolioBacktestRequest(BaseModel):
    ts_codes: list[str]
    days: int = Field(default=240, ge=30, le=1000)
    initial_capital: float = Field(default=100000, ge=10000)
    position_pct: float = Field(default=0.2, ge=0.05, le=1.0)
    stop_loss_pct: float = Field(default=0.07, ge=0.01, le=0.5)
    take_profit_pct: float = Field(default=0.15, ge=0.01, le=1.0)


class BacktestTradeItem(BaseModel):
    entry_date: str
    entry_price: float
    exit_date: str | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    pnl_pct: float = 0
    holding_days: int = 0


class BacktestSummary(BaseModel):
    """US 沿用既有数值；CN 不可计算的比例/收益统计为 null（原因见 metric_unavailable_reasons）"""

    total_trades: int = 0
    win_rate: float | None = 0
    profit_factor: float | None = 0
    total_return: float | None = 0
    max_drawdown: float | None = 0
    sharpe_ratio: float | None = 0
    avg_holding_days: float | None = 0
    annualized_return: float | None = 0
    win_count: int | None = 0
    avg_pnl: float | None = 0
    max_win: float | None = 0
    max_loss: float | None = 0


class BacktestMarketMeta(BaseModel):
    workspace_market: str = "US"
    simulation_mode: str = "backtest"  # CN 固定 strategy_simulation
    price_basis: str = "source"  # CN 固定 qfq（前复权参考价）
    assumptions: list[str] = []
    metric_unavailable_reasons: dict[str, str] = {}


class BacktestResponse(BacktestMarketMeta):
    ts_code: str
    summary: BacktestSummary
    equity_curve: list[list] = []  # [[date, value], ...]
    trades: list[BacktestTradeItem] = []
    data_version: str = ""
    data_date: str | None = None


class PortfolioBacktestResponse(BacktestMarketMeta):
    summary: BacktestSummary
    equity_curve: list[list] = []
    trades: list[dict] = []
    per_stock: list[BacktestResponse] = []
    data_version: str = ""
    data_date: str | None = None
