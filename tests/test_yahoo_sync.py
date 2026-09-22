"""
Yahoo Finance 美股/港股日线同步适配器测试

覆盖：代码映射、Chart JSON 解析（gmtoffset 时区换算）、amount/pct_chg 口径、
增量写库、None 行（停牌）过滤、异常透传。
"""

import pytest

from tests.conftest import write_klines_to_db


# ==================== 代码映射 ====================


def test_to_yahoo_symbol_plain_us():
    from modules.yahoo_sync import to_yahoo_symbol

    assert to_yahoo_symbol("AAPL.US") == "AAPL"
    assert to_yahoo_symbol("NVDA.US") == "NVDA"


def test_to_yahoo_symbol_index_mapping():
    from modules.yahoo_sync import to_yahoo_symbol

    assert to_yahoo_symbol("SPX.US") == "^GSPC"
    assert to_yahoo_symbol("DJI.US") == "^DJI"
    assert to_yahoo_symbol("IXIC.US") == "^IXIC"


def test_to_yahoo_symbol_hk_strips_leading_zero():
    from modules.yahoo_sync import to_yahoo_symbol

    assert to_yahoo_symbol("02331.HK") == "2331.HK"
    assert to_yahoo_symbol("09992.HK") == "9992.HK"


# ==================== Chart JSON 解析 ====================


def _chart_payload(timestamps, quote_rows, gmtoffset):
    """构造 Yahoo Chart API 响应体"""
    return {
        "chart": {
            "result": [
                {
                    "meta": {"gmtoffset": gmtoffset},
                    "timestamp": timestamps,
                    "indicators": {"quote": [quote_rows]},
                }
            ],
            "error": None,
        }
    }


def test_parse_bars_converts_timestamp_with_gmtoffset():
    """美股夏令时 UTC-4：epoch+gmtoffset 换算为交易所当地日期"""
    from modules.yahoo_sync import parse_chart_bars

    # 2026-09-18 13:30 UTC（美东 09:30）= 1789738200
    payload = _chart_payload(
        [1789738200],
        {"open": [337.91], "high": [338.49], "low": [332.53], "close": [336.13], "volume": [86433100]},
        -14400,
    )
    bars = parse_chart_bars(payload, "AAPL.US")
    assert len(bars) == 1
    assert bars[0]["trade_date"] == "20260918"
    assert bars[0]["close"] == 336.13
    assert bars[0]["vol"] == 86433100


def test_parse_bars_skips_null_rows():
    """停牌/缺失行（None 值）必须跳过，不能写入 0 值"""
    from modules.yahoo_sync import parse_chart_bars

    payload = _chart_payload(
        [1789651800, 1789738200],
        {"open": [100.0, None], "high": [101.0, None], "low": [99.0, None], "close": [100.5, None], "volume": [1000, None]},
        -14400,
    )
    bars = parse_chart_bars(payload, "AAPL.US")
    assert len(bars) == 1
    assert bars[0]["close"] == 100.5


def test_parse_bars_empty_result():
    from modules.yahoo_sync import parse_chart_bars

    assert parse_chart_bars({"chart": {"result": [{}], "error": None}}, "AAPL.US") == []
    assert parse_chart_bars({"chart": {"result": None, "error": None}}, "AAPL.US") == []


# ==================== 写库口径 ====================


class FakeSession:
    """可编程的 HTTP 会话：按调用顺序返回预设 payload 或抛异常"""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_sync_us_daily_writes_bars_with_derived_fields(temp_db, db_conn):
    """写库字段口径：amount=close*vol，pct_chg 相对前收并保留 4 位小数"""
    from modules.yahoo_sync import sync_us_daily

    write_klines_to_db(
        db_conn,
        [{"ts_code": "AAPL.US", "trade_date": "20260917", "open": 334.77, "high": 338.34, "low": 330.18, "close": 337.0, "vol": 36700200, "amount": 12367967400.0, "pct_chg": 1.3808}],
    )
    payload = _chart_payload(
        [1789738200],
        {"open": [337.91], "high": [338.49], "low": [332.53], "close": [336.13], "volume": [86433100]},
        -14400,
    )
    session = FakeSession([FakeResponse(payload)])

    added = sync_us_daily("AAPL.US", "20260918", "20260918", session=session)

    assert added == 1
    row = db_conn.execute(
        "SELECT close, vol, amount, pct_chg FROM daily_kline WHERE ts_code='AAPL.US' AND trade_date='20260918'"
    ).fetchone()
    assert row[0] == 336.13
    assert row[1] == 86433100
    assert row[2] == pytest.approx(336.13 * 86433100)
    # (336.13-337.0)/337.0*100 = -0.2582
    assert row[3] == pytest.approx(-0.2582, abs=1e-4)


def test_sync_us_daily_filters_out_of_range_bars(temp_db, db_conn):
    """区间外的 bar 不写入；返回实际新增条数"""
    from modules.yahoo_sync import sync_us_daily

    payload = _chart_payload(
        [1789651800, 1789738200],  # 0917 / 0918
        {"open": [334.0, 337.91], "high": [338.0, 338.49], "low": [330.0, 332.53], "close": [337.0, 336.13], "volume": [100, 200]},
        -14400,
    )
    session = FakeSession([FakeResponse(payload)])

    added = sync_us_daily("AAPL.US", "20260918", "20260918", session=session)

    assert added == 1
    count = db_conn.execute("SELECT COUNT(*) FROM daily_kline WHERE ts_code='AAPL.US'").fetchone()[0]
    assert count == 1
    # 首根也必须使用响应中区间前的前收，不能因为空库而写成 0。
    pct = db_conn.execute("SELECT pct_chg FROM daily_kline").fetchone()[0]
    assert pct == pytest.approx(-0.2582, abs=1e-4)


def test_parse_chart_error_is_failure_not_empty():
    from modules.yahoo_sync import parse_chart_bars

    with pytest.raises(ValueError, match="Not Found"):
        parse_chart_bars({"chart": {"result": None, "error": {"code": "Not Found"}}}, "AAPL.US")


def test_fetch_bounds_are_utc():
    from datetime import datetime, timezone
    from modules.yahoo_sync import fetch_chart

    session = FakeSession([FakeResponse({})])
    fetch_chart("AAPL", "20260918", "20260918", session)
    params = session.calls[0][1]
    assert params["period2"] == int(datetime(2026, 9, 19, tzinfo=timezone.utc).timestamp())


def test_sync_us_daily_raises_on_http_error(temp_db):
    """请求失败必须抛异常（上层据此重试），不能吞掉"""
    from modules.yahoo_sync import sync_us_daily

    session = FakeSession([ConnectionError("proxy unreachable")])
    with pytest.raises(ConnectionError):
        sync_us_daily("AAPL.US", "20260918", "20260918", session=session)


def test_sync_us_daily_hk_stock(temp_db, db_conn):
    """港股：代码去前导零映射 + 香港时区（UTC+8）日期换算"""
    from modules.yahoo_sync import sync_us_daily

    # 2026-09-18 01:30 UTC（香港 09:30）= 1789695000
    payload = _chart_payload(
        [1789695000],
        {"open": [12.67], "high": [12.84], "low": [12.55], "close": [12.57], "volume": [13366635]},
        28800,
    )
    session = FakeSession([FakeResponse(payload)])

    added = sync_us_daily("02331.HK", "20260918", "20260918", session=session)

    assert added == 1
    url, params = session.calls[0]
    assert "2331.HK" in url
    row = db_conn.execute(
        "SELECT trade_date, close FROM daily_kline WHERE ts_code='02331.HK'"
    ).fetchone()
    assert row[0] == "20260918"
    assert row[1] == 12.57
