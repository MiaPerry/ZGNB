"""麒麟会涨停条件：CN 缺源端限价时为不可判定，其他市场保持原阈值"""

import pytest

from modules.indicators import DailyData
from modules.indicators.core import LIMIT_UP_UNKNOWN_REASON
from modules.indicators.kirin_detector import detect_kirin_stage


def _klines(ts_code, limit_up_days=()):
    rows, price = [], 100.0
    for i in range(80):
        prev, pct = price, 10.0 if i in limit_up_days else 0.3
        price *= 1 + pct / 100
        rows.append(
            DailyData(
                ts_code=ts_code,
                trade_date=f"2026{i:04d}",
                open=prev,
                high=price * 1.01,
                low=prev * 0.99,
                close=price,
                vol=1000 + i,
                amount=price * (1000 + i),
                pct_chg=pct,
                prev_close=prev,
            )
        )
    return rows


@pytest.mark.parametrize("cn_code", ["600519.SH", "688981.SH", "000001.SZ"])
def test_cn_limit_up_unknown_skips_dependent_scores(cn_code):
    cn = detect_kirin_stage(_klines(cn_code, limit_up_days=(70, 75)))
    known = detect_kirin_stage(_klines("00700.HK", limit_up_days=(70, 75)))
    assert cn["indicators"]["limit_up_count"] is None
    assert cn["indicators"]["limit_up_unknown_reason"] == LIMIT_UP_UNKNOWN_REASON
    assert known["indicators"]["limit_up_count"] == 2
    assert cn["scores"]["lasheng"] == known["scores"]["lasheng"] - 20


def test_cn_unknown_is_not_treated_as_no_limit_up():
    cn = detect_kirin_stage(_klines("600519.SH"))
    known = detect_kirin_stage(_klines("00700.HK"))
    assert known["indicators"]["limit_up_count"] == 0
    assert cn["scores"]["xishou"] == known["scores"]["xishou"] - 10
    assert cn["scores"]["luoluo"] == known["scores"]["luoluo"] - 10


def test_us_keeps_big_gain_threshold():
    result = detect_kirin_stage(_klines("AAPL.US", limit_up_days=(70,)))
    assert result["indicators"]["limit_up_count"] == 1
    assert "limit_up_unknown_reason" not in result["indicators"]


@pytest.mark.parametrize("count,text", [(None, "涨停未知"), (0, "涨停0次"), (2, "涨停2次")])
def test_cli_limit_up_text_does_not_print_unknown_as_count(count, text):
    from modules.cli import _limit_up_text

    assert _limit_up_text(count) == text
