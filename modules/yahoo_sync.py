"""
Yahoo Finance 美股/港股日线同步适配器（us 分支专用）

- 通过 Yahoo Chart API 拉取日线，经 Clash 代理访问
- 代码映射：指数（SPX/DJI/IXIC → ^GSPC/^DJI/^IXIC）、港股去前导零
- 日期按响应 meta.gmtoffset 换算为交易所当地日期，不依赖系统时区数据库
- 写库口径：amount = close * vol，pct_chg 相对前收（序列前一根或库内前收）保留 4 位小数
"""

import logging
import os
from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone

from modules.database import get_connection

logger = logging.getLogger(__name__)

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# 指数代码 → Yahoo 符号
_INDEX_SYMBOLS = {
    "SPX.US": "^GSPC",
    "DJI.US": "^DJI",
    "IXIC.US": "^IXIC",
}

# Clash 代理（可用 YAHOO_PROXY 环境变量覆盖）
DEFAULT_PROXY = "http://127.0.0.1:7890"

# 拉取区间向前多带的天数：保证区间首根 bar 能算出 pct_chg
_LOOKBACK_DAYS = 15


def to_yahoo_symbol(ts_code: str) -> str:
    """项目标准代码 → Yahoo 符号"""
    if ts_code in _INDEX_SYMBOLS:
        return _INDEX_SYMBOLS[ts_code]
    if ts_code.endswith(".US"):
        return ts_code[: -len(".US")]
    if ts_code.endswith(".HK"):
        # Yahoo 港股代码为无前导零数字 + .HK（02331.HK → 2331.HK）
        return f"{int(ts_code[: -len('.HK')])}.HK"
    raise ValueError(f"不支持的市场代码: {ts_code}")


def parse_chart_bars(payload: dict, ts_code: str) -> list[dict]:
    """解析 Chart API 响应为 K 线列表；停牌/缺失行（None）跳过"""
    chart = payload.get("chart") or {}
    if chart.get("error"):
        error = chart["error"]
        code = error.get("code") if isinstance(error, dict) else None
        safe_codes = {"Not Found", "Bad Request", "Unauthorized", "Forbidden", "Too Many Requests"}
        detail = code if isinstance(code, str) and code in safe_codes else "数据源返回错误"
        raise ValueError(f"Yahoo Chart 错误: {detail}")
    result = chart.get("result") or []
    if not result or not result[0].get("timestamp"):
        return []

    node = result[0]
    gmtoffset = (node.get("meta") or {}).get("gmtoffset", 0)
    quote = node["indicators"]["quote"][0]

    bars = []
    for i, ts in enumerate(node["timestamp"]):
        close = quote["close"][i]
        if close is None:
            continue
        day = datetime.fromtimestamp(ts + gmtoffset, tz=timezone.utc).strftime("%Y%m%d")
        bars.append(
            {
                "ts_code": ts_code,
                "trade_date": day,
                "open": quote["open"][i],
                "high": quote["high"][i],
                "low": quote["low"][i],
                "close": close,
                "vol": quote["volume"][i] or 0,
            }
        )
    return bars


def _default_session():
    """生产 HTTP 会话：curl_cffi + Chrome 指纹 + Clash 代理"""
    from curl_cffi import requests as curl_requests

    from curl_cffi import CurlOpt

    proxy = os.environ.get("YAHOO_PROXY", DEFAULT_PROXY).strip()
    # libcurl 自身也读取代理环境变量；空 PROXY 禁用隐式代理，空 NOPROXY 禁用隐式绕过。
    try:
        return curl_requests.Session(
            impersonate="chrome",
            proxy=proxy,
            trust_env=False,
            curl_options={CurlOpt.PROXY: proxy, CurlOpt.NOPROXY: ""},
        )
    except Exception:
        raise ConnectionError("Yahoo 会话创建失败；请检查 YAHOO_PROXY 配置。") from None


def fetch_chart(symbol: str, start_date: str, end_date: str, session) -> dict:
    """请求 Chart API（period1/period2 为宽松边界，精确区间由本地过滤）"""
    period1 = int(datetime.strptime(start_date, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()) - _LOOKBACK_DAYS * 86400
    period2 = int((datetime.strptime(end_date, "%Y%m%d").replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp())
    try:
        resp = session.get(
            _CHART_URL.format(symbol=symbol),
            params={
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "includePrePost": "false",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        # 不透出底层异常文本或异常链：其中可能含代理 URL、认证字段或响应正文。
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if type(status) is int and 100 <= status <= 599 else "网络或响应解析错误"
        raise ConnectionError(f"Yahoo 请求失败（{detail}）；请检查 YAHOO_PROXY 与数据源连通性。") from None


def sync_us_daily(ts_code: str, start_date: str, end_date: str, *, session=None) -> int:
    """
    拉取并写入单只美股/港股在 [start_date, end_date] 的日线

    Returns:
        实际写入条数（0 表示数据源未返回区间内新行情）

    Raises:
        请求或解析失败时抛异常（由调用方分级重试）
    """
    with closing(_default_session()) if session is None else nullcontext(session) as active_session:
        payload = fetch_chart(to_yahoo_symbol(ts_code), start_date, end_date, active_session)
    all_bars = sorted(parse_chart_bars(payload, ts_code), key=lambda b: b["trade_date"])
    bars = [b for b in all_bars if start_date <= b["trade_date"] <= end_date]
    if not bars:
        return 0

    # 使用同一行情响应的前收，回补首日不再因空库而丢失涨跌幅。
    previous = [b for b in all_bars if b["trade_date"] < start_date]
    prev_close = previous[-1]["close"] if previous else None
    with get_connection() as conn:
        cursor = conn.cursor()
        for bar in bars:
            if prev_close is not None:
                pc = prev_close
            else:
                row = cursor.execute(
                    "SELECT close FROM daily_kline WHERE ts_code = ? AND trade_date < ? "
                    "ORDER BY trade_date DESC LIMIT 1",
                    (ts_code, bar["trade_date"]),
                ).fetchone()
                pc = row[0] if row else None
            pct_chg = round((bar["close"] - pc) / pc * 100, 4) if pc else 0
            cursor.execute(
                """
                INSERT OR REPLACE INTO daily_kline
                (ts_code, trade_date, open, high, low, close, vol, amount,
                 pct_chg, vol_ratio, is_limit_up, is_limit_down)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_code,
                    bar["trade_date"],
                    bar["open"],
                    bar["high"],
                    bar["low"],
                    bar["close"],
                    bar["vol"],
                    bar["close"] * bar["vol"],
                    pct_chg,
                    None,
                    0,
                    0,
                ),
            )
            prev_close = bar["close"]

    logger.info("Yahoo 日线同步完成: %s, %d 条 (%s-%s)", ts_code, len(bars), start_date, end_date)
    return len(bars)
