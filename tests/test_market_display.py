"""展示币种与资产身份仅从所属库取得，测试不访问真实行情。"""

import pytest

from api.models.stock import KlineChartResponse, StockAnalysisResponse, StockListItem
from api.services import stock_service
from modules.database import get_connection, init_database, market_context


@pytest.mark.parametrize("market", ["US", "CN"])
def test_list_exposes_asset_identity_without_suffix_guessing(market):
    init_database(market)
    with market_context(market):
        with get_connection() as conn:
            rows = [("AAPL.US", "美股"), ("00700.HK", "港股"), ("^GSPC.US", "美股指数")] if market == "US" else [("600519.SH", "主板"), ("000300.SH", "A股指数")]
            conn.executemany("INSERT INTO stock_basic(ts_code,name,market) VALUES (?, '测试', ?)", rows)
            if market == "CN":
                conn.executemany("INSERT INTO cn_asset_metadata(ts_code,asset_type,sources) VALUES (?, ?, '[]')", [("600519.SH", "stock"), ("000300.SH", "index")])
        for code, category in rows:
            item = stock_service.list_stocks(market=category)["items"][0]
            expected_type = "index" if "指数" in category else "stock"
            expected_currency = "CNY" if market == "CN" else "HKD" if category == "港股" else "USD"
            assert item["asset_type"] == expected_type
            assert item["currency"] == expected_currency
            serialized = StockListItem(**item).model_dump()
            assert serialized["asset_type"] == expected_type
            assert serialized["currency"] == expected_currency


def test_cn_display_metadata_uses_explicit_asset_type_not_code_prefix():
    init_database("CN")
    with market_context("CN"):
        with get_connection() as conn:
            conn.execute("INSERT INTO cn_asset_metadata(ts_code,asset_type,sources) VALUES ('000001.SZ', 'stock', '[]')")
            conn.execute("INSERT INTO cn_asset_metadata(ts_code,asset_type,sources) VALUES ('600000.SH', 'index', '[]')")
        assert stock_service.get_asset_display("000001.SZ") == {"asset_type": "stock", "currency": "CNY"}
        assert stock_service.get_asset_display("600000.SH") == {"asset_type": "index", "currency": "CNY"}
        assert stock_service.get_asset_display("000999.SH")["asset_type"] == "unknown"


@pytest.mark.parametrize("model", [StockAnalysisResponse, KlineChartResponse])
def test_analysis_and_chart_preserve_display_metadata(model):
    result = model(ts_code="000300.SH", asset_type="index", currency="CNY").model_dump()
    assert result["asset_type"] == "index"
    assert result["currency"] == "CNY"
