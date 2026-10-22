"""tushare_client.py 测试 — Tushare 中转 API 客户端（mock 模式）"""

import os
import time
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd


# ==================== 初始化测试 ====================


class TestTushareClientInit:
    """TushareClient 初始化逻辑"""

    def test_websearch_mode_no_token_required(self, mock_env_for_tests):
        """websearch 模式下不需要 token"""
        os.environ["DATA_MODE"] = "websearch"
        from modules.tushare_client import TushareClient

        client = TushareClient()
        assert client._pro is None

    def test_jnb_mode_requires_token_or_url(self, mock_env_for_tests):
        """JNB 模式下必须有 token 和 API URL（API URL 先检查）"""
        os.environ["DATA_MODE"] = "jnb"
        os.environ.pop("TUSHARE_TOKEN", None)
        os.environ.pop("TUSHARE_API_URL", None)
        import importlib
        import modules.tushare_client as tc

        importlib.reload(tc)
        with pytest.raises(ValueError):
            tc.TushareClient()

    def test_jnb_mode_requires_api_url(self, mock_env_for_tests):
        """JNB 模式下必须有 API URL"""
        os.environ["DATA_MODE"] = "jnb"
        os.environ["TUSHARE_TOKEN"] = "test_token"
        os.environ.pop("TUSHARE_API_URL", None)
        # 需要 reload 模块以重新读取模块级变量
        import importlib
        import modules.tushare_client as tc

        importlib.reload(tc)
        with pytest.raises(ValueError, match="TUSHARE_API_URL"):
            tc.TushareClient()

    def test_custom_token_override(self, mock_env_for_tests):
        """自定义 token 覆盖环境变量"""
        os.environ["DATA_MODE"] = "websearch"
        from modules.tushare_client import TushareClient

        client = TushareClient(token="custom_token_123")
        assert client.token == "custom_token_123"

    def test_default_rate_limit_interval(self, mock_env_for_tests):
        """默认限流间隔 0.55s"""
        os.environ["DATA_MODE"] = "websearch"
        from modules.tushare_client import TushareClient

        client = TushareClient()
        assert client.min_request_interval == 0.55


# ==================== 限流测试 ====================


class TestRateLimit:
    """_rate_limit 方法"""

    def test_rate_limit_enforces_interval(self, mock_env_for_tests):
        """两次调用间隔不小于 min_request_interval"""
        os.environ["DATA_MODE"] = "websearch"
        from modules.tushare_client import TushareClient

        client = TushareClient()
        client.min_request_interval = 0.1  # 缩短测试时间

        start = time.time()
        client._rate_limit()
        client._rate_limit()
        elapsed = time.time() - start
        assert elapsed >= 0.09  # 允许微小误差

    def test_rate_limit_no_sleep_when_enough_time(self, mock_env_for_tests):
        """间隔足够时不 sleep"""
        os.environ["DATA_MODE"] = "websearch"
        from modules.tushare_client import TushareClient

        client = TushareClient()
        client.min_request_interval = 0.01
        client.last_request_time = time.time() - 10  # 10 秒前

        start = time.time()
        client._rate_limit()
        elapsed = time.time() - start
        assert elapsed < 0.05


# ==================== API 方法测试（mock） ====================


class TestApiMethods:
    """各 API 方法的 mock 测试"""

    @pytest.fixture
    def client(self, mock_env_for_tests):
        os.environ["DATA_MODE"] = "websearch"
        from modules.tushare_client import TushareClient

        c = TushareClient()
        c.min_request_interval = 0  # 测试时不限流
        return c

    def test_get_daily_returns_dataframe(self, client):
        """get_daily 正常返回 DataFrame"""
        mock_df = pd.DataFrame({"trade_date": ["20260115"], "close": [100.0]})
        with patch("modules.tushare_client.ts.pro_bar", return_value=mock_df):
            result = client.get_daily("600519.SH", "20260101", "20260115")
            assert result is not None
            assert len(result) == 1
            assert result.iloc[0]["close"] == 100.0

    def test_get_daily_exception_returns_none(self, client):
        """get_daily 异常时返回 None"""
        with patch("modules.tushare_client.ts.pro_bar", side_effect=Exception("API error")):
            result = client.get_daily("600519.SH", "20260101", "20260115")
            assert result is None

    def test_get_index_daily_returns_dataframe(self, client):
        mock_df = pd.DataFrame({"trade_date": ["20260115"], "close": [3500.0]})
        client._pro = MagicMock()
        client._pro.index_daily.return_value = mock_df
        result = client.get_index_daily("000300.SH", "20260101", "20260115")
        assert len(result) == 1

    def test_get_index_daily_exception_returns_none(self, client):
        client._pro = MagicMock()
        client._pro.index_daily.side_effect = Exception("API error")
        result = client.get_index_daily("000300.SH", "20260101", "20260115")
        assert result is None

    def test_get_realtime_quote_returns_dataframe(self, client):
        mock_df = pd.DataFrame({"TS_CODE": ["600519.SH"], "PRICE": [1800.0]})
        with patch("modules.tushare_client.ts.realtime_quote", return_value=mock_df):
            result = client.get_realtime_quote(["600519.SH"])
            assert len(result) == 1

    def test_get_realtime_quote_exception_returns_none(self, client):
        with patch("modules.tushare_client.ts.realtime_quote", side_effect=Exception("API error")):
            result = client.get_realtime_quote(["600519.SH"])
            assert result is None

    def test_get_moneyflow(self, client):
        mock_df = pd.DataFrame({"ts_code": ["600519.SH"], "buy_sm_amount": [1000.0]})
        client._pro = MagicMock()
        client._pro.moneyflow.return_value = mock_df
        result = client.get_moneyflow("600519.SH", "20260115")
        assert len(result) == 1

    def test_get_stock_basic_with_ts_code(self, client):
        mock_df = pd.DataFrame({"ts_code": ["600519.SH"], "name": ["贵州茅台"]})
        client._pro = MagicMock()
        client._pro.stock_basic.return_value = mock_df
        result = client.get_stock_basic(ts_code="600519.SH")
        assert len(result) == 1

    def test_get_stock_basic_with_name(self, client):
        mock_df = pd.DataFrame({"ts_code": ["600519.SH"], "name": ["贵州茅台"]})
        client._pro = MagicMock()
        client._pro.stock_basic.return_value = mock_df
        result = client.get_stock_basic(name="贵州茅台")
        assert len(result) == 1

    def test_get_stock_basic_exception_returns_none(self, client):
        client._pro = MagicMock()
        client._pro.stock_basic.side_effect = Exception("API error")
        result = client.get_stock_basic(ts_code="600519.SH")
        assert result is None

    def test_get_limit_list(self, client):
        mock_df = pd.DataFrame({"ts_code": ["600519.SH"], "limit": ["U"]})
        client._pro = MagicMock()
        client._pro.limit_list_d.return_value = mock_df
        result = client.get_limit_list("20260115")
        assert len(result) == 1

    def test_get_top_list(self, client):
        mock_df = pd.DataFrame({"ts_code": ["600519.SH"]})
        client._pro = MagicMock()
        client._pro.top_list.return_value = mock_df
        result = client.get_top_list("20260115")
        assert len(result) == 1

    def test_get_financial_data(self, client):
        mock_df = pd.DataFrame({"ts_code": ["600519.SH"], "pe": [30.5]})
        client._pro = MagicMock()
        client._pro.fina_indicator.return_value = mock_df
        result = client.get_financial_data("600519.SH", "20250101", "20260101")
        assert len(result) == 1

    def test_get_trade_cal(self, client):
        mock_df = pd.DataFrame({"exchange": ["SSE"], "is_open": [1]})
        client._pro = MagicMock()
        client._pro.trade_cal.return_value = mock_df
        result = client.get_trade_cal(exchange="SSE", start_date="20260101", end_date="20260115")
        assert len(result) == 1

    def test_get_trade_cal_no_dates(self, client):
        mock_df = pd.DataFrame({"exchange": ["SSE"], "is_open": [1]})
        client._pro = MagicMock()
        client._pro.trade_cal.return_value = mock_df
        result = client.get_trade_cal()
        assert len(result) == 1

    def test_check_connection_success(self, client):
        mock_df = pd.DataFrame({"close": [100.0]})
        with patch("modules.tushare_client.ts.pro_bar", return_value=mock_df):
            assert client.check_connection() is True

    def test_check_connection_failure(self, client):
        with patch("modules.tushare_client.ts.pro_bar", return_value=pd.DataFrame()):
            assert client.check_connection() is False

    def test_check_connection_none(self, client):
        with patch("modules.tushare_client.ts.pro_bar", return_value=None):
            assert client.check_connection() is False


# ==================== CN核心接口契约 ====================


@pytest.mark.parametrize("method,endpoint,args,kwargs,params", [
    ("get_raw_daily", "daily", ("600519.SH", "20210101", "20260922"), {},
     {"ts_code": "600519.SH", "start_date": "20210101", "end_date": "20260922"}),
    ("get_adj_factor", "adj_factor", ("600519.SH", "20210101", "20260922"), {},
     {"ts_code": "600519.SH", "start_date": "20210101", "end_date": "20260922"}),
    ("get_index_weight", "index_weight", ("20260901", "20260922"), {},
     {"index_code": "399300.SZ", "start_date": "20260901", "end_date": "20260922"}),
    ("get_stk_limit", "stk_limit", ("600519.SH", "20210101", "20260922"), {},
     {"ts_code": "600519.SH", "start_date": "20210101", "end_date": "20260922"}),
    ("get_index_daily", "index_daily", ("000300.SH", "20210101", "20260922"), {"raise_on_error": True},
     {"ts_code": "000300.SH", "start_date": "20210101", "end_date": "20260922"}),
    ("get_stock_basic", "stock_basic", (), {"list_status": "D", "raise_on_error": True},
     {"list_status": "D"}),
    ("get_trade_cal", "trade_cal", (), {"start_date": "20210101", "end_date": "20260922", "raise_on_error": True},
     {"exchange": "SSE", "start_date": "20210101", "end_date": "20260922"}),
])
def test_cn_interfaces_use_configured_sdk_and_limiter(monkeypatch, method, endpoint, args, kwargs, params):
    from modules import tushare_client as tc

    client = tc.TushareClient.__new__(tc.TushareClient)
    client._pro = MagicMock()
    expected = pd.DataFrame({"trade_date": ["20260922"]})
    getattr(client._pro, endpoint).return_value = expected
    limiter = MagicMock()
    monkeypatch.setattr(client, "_rate_limit", limiter)
    with patch.object(tc.ts, "pro_bar", side_effect=AssertionError("CN原始日线不得使用pro_bar复权")):
        assert getattr(client, method)(*args, **kwargs) is expected
    getattr(client._pro, endpoint).assert_called_once_with(**params)
    limiter.assert_called_once_with()


@pytest.mark.parametrize("endpoint,method,args,kwargs", [
    ("daily", "get_raw_daily", ("600519.SH", "20210101", "20260922"), {}),
    ("adj_factor", "get_adj_factor", ("600519.SH", "20210101", "20260922"), {}),
    ("index_weight", "get_index_weight", ("20260901", "20260922"), {}),
    ("stk_limit", "get_stk_limit", ("600519.SH", "20210101", "20260922"), {}),
    ("index_daily", "get_index_daily", ("000300.SH", "20210101", "20260922"), {"raise_on_error": True}),
    ("stock_basic", "get_stock_basic", (), {"raise_on_error": True}),
    ("trade_cal", "get_trade_cal", (), {"raise_on_error": True}),
])
def test_cn_interfaces_do_not_swallow_errors(monkeypatch, endpoint, method, args, kwargs):
    from modules import tushare_client as tc

    client = tc.TushareClient.__new__(tc.TushareClient)
    client._pro = MagicMock()
    getattr(client._pro, endpoint).side_effect = RuntimeError("权限不足 token=private-value")
    monkeypatch.setattr(client, "_rate_limit", lambda: None)
    with pytest.raises(tc.TushareDataError, match=endpoint) as error:
        getattr(client, method)(*args, **kwargs)
    assert error.value.kind == "auth"
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize("response", [None, [], {"error": "failed"}])
def test_cn_invalid_response_is_not_empty_success(monkeypatch, response):
    from modules import tushare_client as tc

    client = tc.TushareClient.__new__(tc.TushareClient)
    client._pro = MagicMock()
    client._pro.daily.return_value = response
    monkeypatch.setattr(client, "_rate_limit", lambda: None)
    with pytest.raises(tc.TushareDataError, match="daily"):
        client.get_raw_daily("600519.SH", "20210101", "20260922")


# ==================== 模块级常量 ====================


class TestModuleConstants:
    def test_tushare_api_url_from_env(self, mock_env_for_tests):
        """模块级常量从环境变量读取"""
        os.environ["TUSHARE_API_URL"] = "https://test.example.com"
        import importlib
        import modules.tushare_client as tc

        importlib.reload(tc)
        assert tc.TUSHARE_API_URL == "https://test.example.com"
