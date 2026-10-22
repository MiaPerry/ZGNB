"""免费CN源适配：仅使用临时响应，不读取配置或业务数据库。"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest


def _factor_text(rows=None, total=None, symbol="sh600519"):
    rows = rows if rows is not None else [{"d": "2026-10-19", "f": "2"}, {"d": "2026-10-21", "f": "4"}]
    return f"var {symbol}hfq=" + json.dumps({"total": len(rows) if total is None else total, "data": rows}) + ";"


def test_sparse_factors_expand_only_after_confirmed_effective_date():
    from modules.cn_free_client import parse_sina_factors, expand_sina_factors

    sparse = parse_sina_factors(_factor_text(), "sh600519")
    frame = expand_sina_factors(sparse, "600519.SH", "20261019", "20261022")
    assert frame.trade_date.tolist() == ["20261019", "20261020", "20261021", "20261022"]
    assert frame.adj_factor.tolist() == [2, 2, 4, 4]
    assert set(frame.ts_code) == {"600519.SH"}
    with pytest.raises(ValueError, match="生效"):
        expand_sina_factors(sparse, "600519.SH", "20261018", "20261020")
    with pytest.raises(ValueError):
        expand_sina_factors(sparse, "600519.SH", "20261022", "20261019")


@pytest.mark.parametrize("fault", ["empty", "total", "duplicate", "zero", "nan", "date", "code", "script"])
def test_factor_payload_rejects_corruption_and_remote_code(fault):
    from modules.cn_free_client import parse_sina_factors

    rows = [{"d": "2026-10-19", "f": "2"}, {"d": "2026-10-21", "f": "4"}]
    total, symbol = None, "sh600519"
    if fault == "empty":
        rows = []
    elif fault == "total":
        total = 99
    elif fault == "duplicate":
        rows[1]["d"] = rows[0]["d"]
    elif fault in {"zero", "nan"}:
        rows[0]["f"] = "0" if fault == "zero" else "nan"
    elif fault == "date":
        rows[0]["d"] = "2026-02-30"
    elif fault == "code":
        symbol = "sh600000"
    text = _factor_text(rows, total, symbol)
    if fault == "script":
        text += "globalThis.compromised=true;"
    with pytest.raises(ValueError):
        parse_sina_factors(text, "sh600519")


def test_real_calendar_wrapper_accepts_only_the_known_data_alias():
    from modules.cn_free_client import decode_sina_series

    encoded = ("LC/AAApNDXCw6mHbaPgkryxXv10eAJP1LW0SD39aT7+NV44Xba3PxCgTdrp5BkYVAc11hWvg0c/19UAc7jNtHQ"
               "yWBAu2xmGuZI1NVAc3FepphjnTBw1X4hmGu+ypVAcvFenpBXPqCc6F4ZmGueLFwbIN8QTDXPsCc1FepphjvOoCc8Fepphjv"
               "cgFO3CP00wxXXWhrkUdZrIJpw9X3ThrlEp6hlGc88Kcem0VeFpZM46VV4MrTC2KScKc811U4aLXUdlzINc9lTrwFW3T52KPj0"
               "mDueVFuUR1RtiEoCXfdgFOOSGRXnUhrXWhb0kt6Rk2pU44JV4SrTyU9wSDHPwCnXdP1FuiUM44r7qwdKqcYrIZpw1DqgrlU5"
               "IrHRawxjrwBaqcbrIt9gr3UhDtOpyVNjEnCHPnC3royNWvi0gjHXBXYdRlLbFpdJFueSFcqkK30sSDO+68K46IVOwVkaBX/")
    dates = decode_sina_series(f'var datelist="{encoded}";var KLC_TD_SH=datelist;')
    assert len(dates) == 8796 and dates[-1].startswith("2026-12-31")
    with pytest.raises(ValueError):
        decode_sina_series(f'var datelist="{encoded}";var KLC_TD_SH=fetch("evil");')


def test_calendar_fills_closed_days_but_rejects_unknown_coverage():
    from modules.cn_free_client import build_sina_calendar

    dates = ["2026-10-19T00:00:00.000Z", "2026-10-20T00:00:00.000Z", "2026-10-21T00:00:00.000Z"]
    frame = build_sina_calendar(dates, "20261019", "20261021")
    assert frame.is_open.tolist() == [1, 1, 1]
    with pytest.raises(ValueError):
        build_sina_calendar(dates, "20261018", "20261021")
    with pytest.raises(ValueError):
        build_sina_calendar(dates + dates[:1], "20261019", "20261021")
    dates = ["2026-10-16T00:00:00.000Z", "2026-10-19T00:00:00.000Z"]
    assert build_sina_calendar(dates, "20261016", "20261019").is_open.tolist() == [1, 0, 0, 1]


def test_index_units_are_adapted_once_without_adjusting_points():
    from modules.cn_free_client import build_sina_index_daily
    from modules.data_sync import build_cn_daily_candidate

    rows = [
        {"date": "2026-10-19T00:00:00.000Z", "prevclose": 4000, "open": 4001, "high": 4100,
         "low": 3990, "close": 4050, "volume": 1200000, "amount": 5000000},
        {"date": "2026-10-20T00:00:00.000Z", "open": 4050, "high": 4110,
         "low": 4040, "close": 4100, "volume": 1500000, "amount": 6000000},
    ]
    frame = build_sina_index_daily(rows, "000300.SH", "20261019", "20261020")
    assert frame.vol.tolist() == [12000, 15000]
    assert frame.amount.tolist() == [5000, 6000]
    candidate = build_cn_daily_candidate("000300.SH", frame, asset_type="index")
    assert candidate["bars"][-1].close == 4100
    assert candidate["bars"][-1].vol == 1500000
    assert candidate["bars"][-1].amount == 6000000
    assert candidate["anchor_factor"] is None
    assert frame.pct_chg.iloc[0] == pytest.approx(1.25)
    with pytest.raises(ValueError):
        build_sina_index_daily(rows, "600519.SH", "20261019", "20261020")
    with pytest.raises(ValueError):
        build_sina_index_daily(rows + rows[:1], "000300.SH", "20261019", "20261020")


@pytest.mark.parametrize("text", ['var d="AA==";fetch("https://evil");', 'var d="AA=="; var x=1;', 'var d=(()=>"AA==")();'])
def test_compressed_payload_does_not_execute_remote_javascript(text):
    from modules.cn_free_client import decode_sina_series

    with pytest.raises(ValueError):
        decode_sina_series(text)


def test_client_reuses_factors_and_calendar_but_never_mutates_daily_source(monkeypatch):
    from modules import cn_free_client

    source = pd.DataFrame([{"ts_code": "600519.SH", "trade_date": "20261019", "close": 100, "vol": 20}])
    raw_client = SimpleNamespace(get_raw_daily=Mock(return_value=source))
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        return _factor_text() if url.endswith("hfq.js") else 'var datelist="AA==";'
    monkeypatch.setattr(cn_free_client, "decode_sina_series", lambda text: ["2026-10-19", "2026-10-20", "2026-10-21"])
    client = cn_free_client.FreeCNClient(raw_client=raw_client, fetch_text=fetch)
    client.get_adj_factor("600519.SH", "20261019", "20261020")
    client.get_adj_factor("600519.SH", "20261019", "20261021")
    client.get_trade_cal(start_date="20261019", end_date="20261020")
    client.get_trade_cal(start_date="20261019", end_date="20261021")
    assert len(calls) == 2
    frame = client.get_raw_daily("600519.SH", "20261019", "20261020")
    assert frame.equals(source) and source.vol.iloc[0] == 20
    raw_client.get_raw_daily.assert_called_once_with("600519.SH", "20261019", "20261020")


def test_public_request_has_timeout_size_limit_and_safe_errors(monkeypatch):
    from modules.cn_free_client import public_text, CNFreeSourceError
    import requests

    response = Mock()
    response.status_code = 200
    response.iter_content.return_value = [b"x" * (2 * 1024 * 1024 + 1)]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    get = Mock(return_value=response)
    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(CNFreeSourceError) as error:
        public_text("https://finance.sina.com.cn/example")
    assert error.value.kind == "invalid_response"
    assert get.call_args.kwargs["timeout"] == 30
    get.side_effect = requests.ConnectionError("credential-that-must-not-leak")
    with pytest.raises(CNFreeSourceError) as error:
        public_text("https://finance.sina.com.cn/example")
    assert error.value.kind == "network"
    assert "credential-that-must-not-leak" not in str(error.value)
