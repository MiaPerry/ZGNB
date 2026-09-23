"""股票库接口回归：真实 SQLite 查询，临时库隔离，不调用行情数据源。"""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import write_stock_basic


@pytest.fixture
def stock_client(db_conn):
    from api.main import app

    for code, name, industry, market in [
        ("AAPL.US", "苹果 Apple", "科技", "美股"),
        ("MSFT.US", "Microsoft", "科技", "美股"),
        ("NVDA.US", "NVIDIA", "半导体", "美股"),
        ("EMPTY.US", "暂无行情", "  ", "美股"),
        ("02331.HK", "李宁", "", "港股"),
        ("SPX.US", "标普500指数", "", "美股指数"),
        ("600519.SH", "贵州茅台", "白酒", "主板"),
    ]:
        write_stock_basic(db_conn, code, name, industry, market)
    db_conn.executemany(
        "INSERT INTO daily_kline (ts_code, trade_date, close, pct_chg, vol) VALUES (?, ?, ?, ?, ?)",
        [
            ("AAPL.US", "20260918", 100, 1, 1000),
            ("AAPL.US", "20260921", 105, 5, 2000),
            ("MSFT.US", "20260918", 200, -2, 3000),
            ("NVDA.US", "20260921", 120, 0, 0),
            ("02331.HK", "20260921", 20, 1, 100),
            ("SPX.US", "20260921", 6000, 1, 10000),
            ("600519.SH", "20260922", 1500, 10, 100000),
        ],
    )
    db_conn.execute("INSERT INTO watchlist (ts_code, name) VALUES ('AAPL.US', '苹果')")
    db_conn.commit()
    with TestClient(app) as client:
        yield client


def test_list_defaults_to_us_stocks_and_uses_each_latest_quote(stock_client):
    response = stock_client.get("/api/v1/stock/list")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 4
    assert [s["ts_code"] for s in data["items"]] == ["AAPL.US", "EMPTY.US", "MSFT.US", "NVDA.US"]
    apple, missing, microsoft, nvidia = data["items"]
    assert (apple["close"], apple["pct_chg"], apple["vol"], apple["trade_date"]) == (105, 5, 2000, "20260921")
    assert apple["is_watchlisted"] is True
    assert microsoft["trade_date"] == "20260918"
    assert microsoft["is_watchlisted"] is False
    assert missing["close"] is None
    assert missing["trade_date"] is None
    assert missing["industry"] == ""
    assert missing["data_status"] == "missing"
    assert nvidia["pct_chg"] == 0
    assert nvidia["vol"] == 0
    assert nvidia["data_status"] == "available"


@pytest.mark.parametrize("params, expected", [
    ({"q": " aapl "}, ["AAPL.US"]),
    ({"q": "苹果"}, ["AAPL.US"]),
    ({"q": "micro"}, ["MSFT.US"]),
    ({"q": "%"}, []),
    ({"q": "_"}, []),
    ({"q": "' OR 1=1 --"}, []),
    ({"industry": "科技"}, ["AAPL.US", "MSFT.US"]),
    ({"industry": ""}, ["EMPTY.US"]),
    ({"data_status": "missing"}, ["EMPTY.US"]),
    ({"data_status": "available"}, ["AAPL.US", "MSFT.US", "NVDA.US"]),
    ({"industry": "科技", "q": "苹果", "data_status": "available"}, ["AAPL.US"]),
    ({"market": "港股"}, ["02331.HK"]),
    ({"market": "美股指数"}, ["SPX.US"]),
    ({"sort_by": "pct_chg", "order": "desc"}, ["AAPL.US", "NVDA.US", "MSFT.US", "EMPTY.US"]),
    ({"sort_by": "pct_chg", "order": "asc"}, ["MSFT.US", "NVDA.US", "AAPL.US", "EMPTY.US"]),
    ({"sort_by": "vol", "order": "desc"}, ["MSFT.US", "AAPL.US", "NVDA.US", "EMPTY.US"]),
    ({"sort_by": "ts_code", "order": "desc"}, ["NVDA.US", "MSFT.US", "EMPTY.US", "AAPL.US"]),
])
def test_filter_and_sort(stock_client, params, expected):
    response = stock_client.get("/api/v1/stock/list", params=params)
    assert response.status_code == 200
    data = response.json()
    assert [s["ts_code"] for s in data["items"]] == expected
    assert data["total"] == len(expected)


def test_pagination_and_market_metadata_do_not_depend_on_search(stock_client):
    data = stock_client.get("/api/v1/stock/list", params={"page": 2, "page_size": 2}).json()
    assert data["total"] == 4
    assert data["page"] == 2
    assert data["page_size"] == 2
    assert [s["ts_code"] for s in data["items"]] == ["MSFT.US", "NVDA.US"]
    filtered = stock_client.get("/api/v1/stock/list", params={"q": "不存在"}).json()
    assert filtered["total"] == 0
    assert filtered["items"] == []
    assert filtered["market_total"] == 4
    assert filtered["latest_trade_date"] == "20260921"
    assert set(filtered["industries"]) == {"", "科技", "半导体"}
    assert {m["market"]: m["count"] for m in filtered["markets"]} == {"美股": 4, "港股": 1, "美股指数": 1}
    beyond = stock_client.get("/api/v1/stock/list", params={"page": 999}).json()
    assert beyond["total"] == 4
    assert beyond["items"] == []
    hk = stock_client.get("/api/v1/stock/list", params={"market": "港股"}).json()
    assert hk["industries"] == [""]
    assert hk["market_total"] == 1


@pytest.mark.parametrize("params", [
    {"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "bad"},
    {"market": "主板"}, {"market": "all"}, {"data_status": "bad"},
    {"sort_by": "close; DROP TABLE stock_basic"}, {"order": "bad"}, {"q": "x" * 101},
])
def test_invalid_query_returns_422(stock_client, params):
    assert stock_client.get("/api/v1/stock/list", params=params).status_code == 422


def test_empty_database(temp_db):
    from api.main import app

    with TestClient(app) as client:
        response = client.get("/api/v1/stock/list")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == data["market_total"] == 0
    assert data["latest_trade_date"] is None
    assert data["items"] == data["markets"] == data["industries"] == []


def test_invalid_latest_close_is_not_presented_as_zero_quote(stock_client, db_conn):
    db_conn.execute(
        "INSERT INTO daily_kline (ts_code, trade_date, close, pct_chg, vol) VALUES ('EMPTY.US', '20260921', 0, 0, 0)"
    )
    db_conn.commit()
    data = stock_client.get("/api/v1/stock/list", params={"data_status": "missing"}).json()
    assert data["total"] == 1
    assert data["items"][0]["close"] is None
    assert data["items"][0]["pct_chg"] is None


def test_watchlist_state_follows_existing_add_and_remove_api(stock_client):
    response = stock_client.post("/api/v1/watchlist/", json={"ts_code": "MSFT.US"})
    assert response.status_code == 200
    data = stock_client.get("/api/v1/stock/list", params={"q": "MSFT"}).json()
    assert data["items"][0]["is_watchlisted"] is True
    assert stock_client.delete("/api/v1/watchlist/MSFT.US").status_code == 200
    data = stock_client.get("/api/v1/stock/list", params={"q": "MSFT"}).json()
    assert data["items"][0]["is_watchlisted"] is False
