"""
P1-1 回归测试：DataSyncer 新方法 + modules/report + 4 个薄壳脚本

- sync_missing(ts_codes) 返回 Dict[ts_code, int]
- sync_daily_and_compute() 链式调用 sync_all_daily_kline + sync_all_indicators
- report.assess_watchlist([]) 返回 []
- report.render_assessment([]) 渲染空报告（含表头 + 元信息 + 0 指标）
- 4 个薄壳脚本能 --help 退出 0
"""

import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("market,code", [("US", "AAPL.US"), ("CN", "600519.SH")])
def test_script_watchlist_uses_selected_database(temp_db, monkeypatch, market, code):
    from modules.database import add_watchlist_item, init_database, market_context
    from scripts._common import load_watchlist

    monkeypatch.delenv("STOCKS_JSON", raising=False)
    add_watchlist_item("AAPL.US")
    with market_context("CN"):
        init_database()
        add_watchlist_item("600519.SH")
    with market_context(market):
        assert load_watchlist() == [code]


@pytest.mark.parametrize("market,raw,expected", [
    ("US", "AAPL.US", ["AAPL.US"]),
    ("US", "00700.HK", ["00700.HK"]),
    ("CN", "600519", ["600519.SH"]),
    ("CN", "000001", ["000001.SZ"]),
])
def test_script_watchlist_explicit_json_is_validated(tmp_path, monkeypatch, market, raw, expected):
    import json
    from modules.database import market_context
    from scripts._common import load_watchlist

    path = tmp_path / "名单.json"
    path.write_text(json.dumps([{"code": raw}, {"code": raw}]), encoding="utf-8")
    monkeypatch.setenv("STOCKS_JSON", str(path))
    with market_context(market):
        assert load_watchlist() == expected


@pytest.mark.parametrize("market,code", [("US", "600519.SH"), ("CN", "AAPL.US")])
def test_script_watchlist_rejects_wrong_market_json(tmp_path, monkeypatch, market, code):
    import json
    from modules.database import MarketCodeError, market_context
    from scripts._common import load_watchlist

    path = tmp_path / "wrong.json"
    path.write_text(json.dumps([{"code": code}]), encoding="utf-8")
    monkeypatch.setenv("STOCKS_JSON", str(path))
    with market_context(market), pytest.raises(MarketCodeError):
        load_watchlist()


def test_script_watchlist_missing_cn_does_not_create_database(monkeypatch):
    from modules.database import MarketUnavailableError, get_db_path, market_context
    from scripts._common import load_watchlist

    monkeypatch.delenv("STOCKS_JSON", raising=False)
    with market_context("CN"), pytest.raises(MarketUnavailableError):
        load_watchlist()
    assert not get_db_path("CN").exists()


# ==================== CN成分快照 ====================


def _cn_weight_frame(date, count=300):
    import pandas as pd

    return pd.DataFrame([{"index_code": "399300.SZ", "con_code": f"{600000 + i:06d}.SH",
                          "trade_date": date, "weight": 100 / 300} for i in range(count)])


def test_cn_snapshot_chooses_latest_complete_date_without_union():
    import pandas as pd
    from modules import data_sync

    frame = pd.concat([_cn_weight_frame("20261016"), _cn_weight_frame("20261020", 299),
                       _cn_weight_frame("20261022"), _cn_weight_frame("20260922")])
    result = data_sync.select_cn_constituent_snapshot(frame, "20261021", {"20261016", "20261020", "20260922"})
    assert result["trade_date"] == "20261016"
    assert len(result["codes"]) == len(set(result["codes"])) == 300
    assert result["source"] == "tushare.index_weight:399300.SZ"
    assert len(result["sha256"]) == 64
    reordered = data_sync.select_cn_constituent_snapshot(frame.iloc[::-1], "20261021",
                                                        {"20261016", "20261020", "20260922"})
    assert reordered == result


@pytest.mark.parametrize("defect", ["partial", "duplicate", "stale", "future", "closed", "index", "weight"])
def test_cn_snapshot_rejects_incomplete_or_invalid_source(defect):
    import pandas as pd
    from modules import data_sync

    frame = _cn_weight_frame("20261016")
    open_dates = {"20261016", "20260819", "20261022"}
    if defect == "partial":
        frame = frame.iloc[:-1]
    elif defect == "duplicate":
        frame = pd.concat([frame, frame.iloc[:1]])
    elif defect == "stale":
        frame["trade_date"] = "20260819"
    elif defect == "future":
        frame["trade_date"] = "20261022"
    elif defect == "closed":
        open_dates = set()
    elif defect == "index":
        frame.loc[0, "con_code"] = "000300.SH"
    else:
        frame.loc[0, "weight"] = float("nan")
    with pytest.raises(ValueError, match="沪深300.*快照"):
        data_sync.select_cn_constituent_snapshot(frame, "20261021", open_dates)


@pytest.mark.parametrize("defect", ["exchange", "partial", "headers", "duplicate", "date", "weight", "oversized"])
def test_cn_csindex_snapshot_preserves_source_and_validates_exchange(monkeypatch, defect):
    from types import SimpleNamespace
    from modules import data_sync
    import xlrd

    headers = ["日期Date", "指数代码 Index Code", "指数名称 Index Name", "Index Name(Eng)",
               "成份券代码Constituent Code", "Constituent Name", "Constituent Name(Eng)",
               "Exchange", "交易所英文名称Exchange(Eng)", "权重(%)weight"]
    rows = [headers] + [["20260930", "000300", "沪深300", "CSI 300", f"{600000+i:06d}",
                       "名称", "Name", "上海", "Shanghai Stock Exchange", 100 / 300] for i in range(300)]
    rows[1][4], rows[1][8] = "000001", "Shenzhen Stock Exchange"
    sheet = SimpleNamespace(nrows=len(rows), ncols=10, row_values=lambda n: rows[n])
    monkeypatch.setattr(xlrd, "open_workbook", lambda **kw: SimpleNamespace(sheet_by_index=lambda n: sheet))
    snapshot = data_sync.parse_csindex_closeweight(b"xls", "20261021", {"20260930"})
    assert snapshot["source"] == "csindex.closeweight:000300"
    assert snapshot["trade_date"] == "20260930"
    assert "000001.SZ" in snapshot["codes"] and len(snapshot["codes"]) == 300
    data_sync.build_cn_seed_assets({}, snapshot, [])
    content = b"xls"
    if defect == "exchange":
        rows[1][8] = "Unknown Exchange"
    elif defect == "partial":
        sheet.nrows = 300
    elif defect == "headers":
        rows[0][9] = "Unknown column"
    elif defect == "duplicate":
        rows[2] = rows[1]
    elif defect == "date":
        rows[1][0] = "20261022"
    elif defect == "weight":
        rows[1][9] = -1
    else:
        content = b"x" * (2 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        data_sync.parse_csindex_closeweight(content, "20261021", {"20260930"})


@pytest.mark.parametrize("valid", [True, False])
def test_cn_snapshot_monthly_lookup_is_bounded_and_fixed(valid):
    import pandas as pd
    from unittest.mock import Mock
    from modules import data_sync

    dates = pd.date_range("20260801", "20261021")
    client = Mock()
    client.get_trade_cal.return_value = pd.DataFrame({"cal_date": dates.strftime("%Y%m%d"),
                                                    "is_open": [int(d.weekday() < 5) for d in dates]})
    client.get_index_weight.side_effect = [pd.DataFrame(),
                                           _cn_weight_frame("20260922") if valid else pd.DataFrame(),
                                           _cn_weight_frame("20260819")]
    if valid:
        result = data_sync.load_cn_constituent_snapshot(client, "20261021")
        assert result["trade_date"] == "20260922"
        assert client.get_index_weight.call_count == 2
    else:
        with pytest.raises(ValueError, match="三个月"):
            data_sync.load_cn_constituent_snapshot(client, "20261021")
        assert client.get_index_weight.call_count == 3
    assert [call.args for call in client.get_index_weight.call_args_list] == [
        ("20261001", "20261021"), ("20260901", "20260930"), ("20260801", "20260831")][:client.get_index_weight.call_count]
    client.get_trade_cal.assert_called_once_with(start_date="20260801", end_date="20261021", raise_on_error=True)


def test_cn_seed_preserves_all_legacy_references_and_filters_basic(temp_db):
    from modules import data_sync
    from modules.database import get_connection

    with get_connection() as conn:
        conn.executemany("INSERT INTO stock_basic(ts_code,name,market) VALUES (?,?,?)", [
            ("688981.SH", "旧科创股", "科创板"), ("600010.SH", "旧成分", "主板"), ("AAPL.US", "Apple", "美股")])
        conn.execute("INSERT INTO daily_kline(ts_code,trade_date,close) VALUES ('002415.SZ','20210101',1)")
        conn.execute("INSERT INTO watchlist(ts_code,name) VALUES ('600176.SH','仅自选引用')")
        conn.execute("INSERT INTO tracking_pool_self(ts_code,add_date,notes) VALUES ('300001.SZ','20210101','保留')")
        legacy = data_sync.collect_legacy_cn_assets(conn)
    assert set(legacy) == {"688981.SH", "600010.SH", "002415.SZ", "600176.SH", "300001.SZ"}
    snapshot = data_sync.select_cn_constituent_snapshot(_cn_weight_frame("20261016"), "20261021", {"20261016"})
    basic = [{"ts_code": "600000.SH", "name": "浦发银行", "market": "主板", "list_status": "L"},
             {"ts_code": "603999.SH", "name": "名单外", "market": "主板"}]
    pool = data_sync.build_cn_seed_assets(legacy, snapshot, basic)
    by_code = {row["ts_code"]: row for row in pool}
    assert len(pool) == len(by_code) == len(set(legacy) | set(snapshot["codes"]) | {"000300.SH"}) == 304
    assert "603999.SH" not in by_code
    assert by_code["600010.SH"]["sources"] == ["hs300", "legacy"]
    assert by_code["688981.SH"]["name"] == "旧科创股"
    assert by_code["688981.SH"]["market"] == "科创板"
    assert by_code["002415.SZ"]["metadata_status"] == "missing"
    assert by_code["000300.SH"]["asset_type"] == "index"
    assert not by_code["000300.SH"]["is_hs300"]
    assert sum(row["is_hs300"] for row in pool) == 300


@pytest.mark.parametrize("field", ["codes", "weights", "trade_date"])
def test_cn_seed_rejects_modified_snapshot(field):
    from modules import data_sync

    snapshot = data_sync.select_cn_constituent_snapshot(_cn_weight_frame("20261016"), "20261021", {"20261016"})
    if field == "codes":
        snapshot["codes"].pop()
    elif field == "weights":
        snapshot["weights"]["600000.SH"] = 1.0
    else:
        snapshot["trade_date"] = "20261020"
    with pytest.raises(ValueError, match="快照.*摘要"):
        data_sync.build_cn_seed_assets({}, snapshot, [])


def test_cn_asset_registry_is_idempotent_and_includes_manual_non_seed(temp_db):
    from modules import data_sync
    from modules.database import get_connection, init_database, market_context

    snapshot = data_sync.select_cn_constituent_snapshot(_cn_weight_frame("20261016"), "20261021", {"20261016"})
    pool = data_sync.build_cn_seed_assets({"688981.SH": {"name": "旧科创股", "market": "科创板"}}, snapshot, [])
    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            data_sync.register_cn_assets(conn, pool)
            data_sync.register_cn_assets(conn, pool)
            data_sync.register_cn_assets(conn, [{"ts_code": "601999.SH", "name": "手动新增", "market": "主板",
                                                "asset_type": "stock", "sources": ["manual"]}])
            assert conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0] == 303
            assert conn.execute("SELECT COUNT(*) FROM cn_asset_metadata").fetchone()[0] == 303
            manual = dict(conn.execute("SELECT * FROM cn_asset_metadata WHERE ts_code='601999.SH'").fetchone())
            assert not manual["is_seed"] and not manual["is_hs300"]
            codes = data_sync.collect_cn_sync_codes(conn)
            assert "601999.SH" in codes and "688981.SH" in codes
            assert "000300.SH" not in codes
            assert len(codes) == 302
    with get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0] == 0
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='cn_asset_metadata'").fetchone()


def test_cn_membership_refresh_does_not_remove_originals_or_personal_records(temp_db):
    from modules import data_sync
    from modules.database import get_connection, init_database, market_context

    first = data_sync.select_cn_constituent_snapshot(_cn_weight_frame("20261016"), "20261021", {"20261016"})
    changed = _cn_weight_frame("20261020")
    changed.loc[299, "con_code"] = "600300.SH"
    second = data_sync.select_cn_constituent_snapshot(changed, "20261021", {"20261020"})
    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            data_sync.register_cn_assets(conn, data_sync.build_cn_seed_assets({}, first, []))
            conn.execute("INSERT INTO watchlist(ts_code,name,notes) VALUES ('600299.SH','旧成员','不删')")
            data_sync.register_cn_assets(conn, data_sync.build_cn_seed_assets({}, second, []), refresh_constituents=True)
            assert conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0] == 302
            old = dict(conn.execute("SELECT * FROM cn_asset_metadata WHERE ts_code='600299.SH'").fetchone())
            added = dict(conn.execute("SELECT * FROM cn_asset_metadata WHERE ts_code='600300.SH'").fetchone())
            assert old["is_seed"] and not old["is_hs300"]
            assert added["is_hs300"] and not added["is_seed"]
            assert conn.execute("SELECT COUNT(*) FROM cn_asset_metadata WHERE is_hs300=1").fetchone()[0] == 300
            assert conn.execute("SELECT notes FROM watchlist").fetchone()[0] == "不删"


# ==================== CN行情候选与事务发布 ====================


def _cn_daily_source(code="600519.SH"):
    import pandas as pd

    daily = pd.DataFrame([
        {"ts_code": code, "trade_date": "20261019", "open": 100, "high": 110, "low": 90,
         "close": 105, "vol": 12, "amount": 123, "pct_chg": 5},
        {"ts_code": code, "trade_date": "20261020", "open": 50, "high": 60, "low": 40,
         "close": 55, "vol": 15, "amount": 88, "pct_chg": 4.76},
    ])
    factors = pd.DataFrame([
        {"ts_code": code, "trade_date": "20261019", "adj_factor": 1.0},
        {"ts_code": code, "trade_date": "20261020", "adj_factor": 2},
        {"ts_code": code, "trade_date": "20261021", "adj_factor": 99},
    ])
    return daily, factors


def test_cn_candidate_uses_confirmed_anchor_and_converts_source_units_once():
    from modules import data_sync

    daily, factors = _cn_daily_source()
    candidate = data_sync.build_cn_daily_candidate("600519.SH", daily, factors)
    assert candidate["anchor_date"] == "20261020" and candidate["anchor_factor"] == 2
    first, last = candidate["bars"]
    assert (first.open, first.high, first.low, first.close) == (50, 55, 45, 52.5)
    assert last.close == 55 and last.pct_chg == 4.76
    assert (first.vol, first.amount) == (1200, 123000)
    assert candidate["raw_rows"][0]["close"] == 105
    assert {row["trade_date"] for row in candidate["factor_rows"]} == {"20261019", "20261020"}
    assert daily.iloc[0]["vol"] == 12 and daily.iloc[0]["amount"] == 123
    assert data_sync.build_cn_daily_candidate("600519.SH", daily, factors) == candidate


@pytest.mark.parametrize("fault", [None, "indicator", "revision"])
def test_cn_publish_is_atomic_and_rebuild_overwrites_existing_dates(temp_db, monkeypatch, fault):
    from modules import data_sync, data_freshness
    from modules.database import get_connection, init_database, market_context
    from modules.indicators import cache_builder

    code = "600519.SH"
    daily, factors = _cn_daily_source(code)
    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            data_sync.register_cn_assets(conn, [{"ts_code": code, "asset_type": "stock", "sources": ["manual"]}])
        first = data_sync.build_cn_daily_candidate(code, daily, factors)
        version = data_freshness.get_data_version(code)
        data_sync.publish_cn_daily_candidate(first, version)
        old_version = data_freshness.get_data_version(code)
        assert old_version != version
        tables = ("cn_raw_daily", "cn_adj_factor", "cn_daily_publication", "daily_kline", "indicator_cache", "sync_log")
        with get_connection() as conn:
            before = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in tables}
        factors.loc[0, "adj_factor"] = 1.5
        revised = data_sync.build_cn_daily_candidate(code, daily, factors)
        if fault == "indicator":
            real_write = cache_builder.persist_indicator_rows
            def fail(conn, rows, **kwargs):
                real_write(conn, rows, **kwargs)
                raise RuntimeError("指标发布故障")
            monkeypatch.setattr(cache_builder, "persist_indicator_rows", fail)
        elif fault == "revision":
            real_revision = data_freshness.record_data_change
            def fail(conn, ts_code, date):
                real_revision(conn, ts_code, date)
                raise RuntimeError("版本发布故障")
            monkeypatch.setattr(data_freshness, "record_data_change", fail)
        if fault:
            with pytest.raises(RuntimeError, match="发布故障"):
                data_sync.publish_cn_daily_candidate(revised, old_version)
            with get_connection() as conn:
                assert {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in tables} == before
            assert data_freshness.get_data_version(code) == old_version
        else:
            data_sync.publish_cn_daily_candidate(revised, old_version)
            with get_connection() as conn:
                prices = conn.execute("SELECT k.close,i.close FROM daily_kline k JOIN indicator_cache i "
                                      "USING(ts_code,trade_date) ORDER BY trade_date").fetchall()
                assert [tuple(row) for row in prices] == [(78.75, 78.75), (55, 55)]
                assert conn.execute("SELECT COUNT(*) FROM cn_adj_factor").fetchone()[0] == 2
            assert data_freshness.get_data_version(code) != old_version


def test_cn_index_publishes_points_without_stock_factor_requirement(temp_db):
    from modules import data_sync
    from modules.data_freshness import get_data_status, get_data_version
    from modules.database import get_connection, init_database, market_context

    code = "000300.SH"
    daily, _ = _cn_daily_source(code)
    candidate = data_sync.build_cn_daily_candidate(code, daily, None, asset_type="index")
    assert candidate["factor_rows"] == [] and candidate["anchor_factor"] is None
    assert candidate["bars"][0].close == 105
    assert candidate["bars"][0].amount == 123000
    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            data_sync.register_cn_assets(conn, [{"ts_code": code, "asset_type": "index", "sources": ["reference"]}])
        data_sync.publish_cn_daily_candidate(candidate, get_data_version(code))
        assert get_data_status(code)["stocks"][code]["ready"]
        with get_connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM cn_adj_factor").fetchone()[0] == 0
            assert conn.execute("SELECT anchor_factor FROM cn_daily_publication").fetchone()[0] is None


@pytest.mark.parametrize("defect", ["price", "basis", "factor_code", "anchor"])
def test_cn_publish_rejects_candidate_that_no_longer_matches_raw_basis(temp_db, defect):
    from modules import data_sync
    from modules.data_freshness import get_data_version
    from modules.database import get_connection, init_database, market_context

    code = "600519.SH"
    daily, factors = _cn_daily_source(code)
    candidate = data_sync.build_cn_daily_candidate(code, daily, factors)
    if defect == "price":
        candidate["bars"][0].close = 999
    elif defect == "basis":
        candidate["raw_rows"][0]["basis_version"] = "unknown"
    elif defect == "factor_code":
        candidate["factor_rows"][0]["ts_code"] = "600000.SH"
    else:
        candidate["anchor_date"] = "20261021"
    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            data_sync.register_cn_assets(conn, [{"ts_code": code, "asset_type": "stock", "sources": ["manual"]}])
        with pytest.raises(ValueError, match="候选.*口径"):
            data_sync.publish_cn_daily_candidate(candidate, get_data_version(code))
        with get_connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM cn_adj_factor").fetchone()[0] == 0


@pytest.mark.parametrize("table", ["cn_adj_factor", "cn_raw_daily"])
def test_cn_transaction_rechecks_stored_raw_and_factors_before_revision(temp_db, table):
    from modules import data_sync
    from modules.data_freshness import get_data_version
    from modules.database import get_connection, init_database, market_context

    code = "600519.SH"
    daily, factors = _cn_daily_source(code)
    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            data_sync.register_cn_assets(conn, [{"ts_code": code, "asset_type": "stock", "sources": ["manual"]}])
            conn.execute(f"CREATE TRIGGER drop_one BEFORE INSERT ON {table} "
                         "WHEN NEW.trade_date='20261019' BEGIN SELECT RAISE(IGNORE); END")
        version = get_data_version(code)
        with pytest.raises(RuntimeError, match="事务内.*校验"):
            data_sync.publish_cn_daily_candidate(data_sync.build_cn_daily_candidate(code, daily, factors), version)
        assert get_data_version(code) == version
        with get_connection() as conn:
            for name in ("cn_raw_daily", "cn_adj_factor", "indicator_cache", "daily_kline", "cn_daily_publication"):
                assert conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0


def test_cn_merge_keeps_normalized_old_units_and_reanchors_all_history():
    from modules import data_sync

    code = "600519.SH"
    daily, factors = _cn_daily_source(code)
    old = data_sync.build_cn_daily_candidate(code, daily, factors)
    changed = factors.copy()
    changed.loc[0, "adj_factor"] = 1.5
    merged = data_sync.merge_cn_daily_candidate(code, daily.iloc[-1:], changed, stored_raw=old["raw_rows"])
    assert merged["bars"][0].close == 78.75
    assert merged["bars"][0].vol == 1200 and merged["bars"][0].amount == 123000
    assert old["bars"][0].close == 52.5
    assert data_sync.merge_cn_daily_candidate(code, daily.iloc[-1:], changed, stored_raw=merged["raw_rows"]) == merged
    with pytest.raises(ValueError, match="因子.*覆盖"):
        data_sync.merge_cn_daily_candidate(code, daily.iloc[-1:], changed.iloc[1:], stored_raw=old["raw_rows"])


def test_cn_daily_request_rechecks_last_five_stored_dates_and_preserves_earlier_start():
    from modules import data_sync

    dates = ["20261001", "20261009", "20261012", "20261013", "20261014", "20261015", "20261016"]
    assert data_sync.cn_daily_request_start("20211020", dates) == "20261012"
    assert data_sync.cn_daily_request_start("20211020", dates, history_repair=True) == "20211020"
    assert data_sync.cn_daily_request_start("20261005", dates, history_repair=True) == "20261001"
    assert data_sync.cn_daily_request_start("20211020", []) == "20211020"
    assert data_sync.cn_daily_request_start("20211020", dates[:2]) == "20261001"


@pytest.mark.parametrize("gap", ["none", "unknown", "suspended", "intraday", "calendar", "factor", "future"])
def test_cn_segmented_history_requires_explained_daily_coverage(gap):
    from datetime import datetime, timedelta
    import pandas as pd
    from modules import data_sync

    code = "600519.SH"
    calls = []
    class Client:
        def get_trade_cal(self, **kwargs):
            assert kwargs["raise_on_error"]
            first, last = datetime.strptime(kwargs["start_date"], "%Y%m%d"), datetime.strptime(kwargs["end_date"], "%Y%m%d")
            days = [(first + timedelta(days=n)).strftime("%Y%m%d") for n in range((last - first).days + 1)]
            if gap == "calendar":
                days = days[1:]
            return pd.DataFrame([{"cal_date": day, "is_open": 1} for day in days])
        def get_raw_daily(self, ts_code, start_date, end_date):
            calls.append(("daily", start_date, end_date))
            dates = [day for day in ["20260930", "20261001", "20261002"] if start_date <= day <= end_date]
            if gap in {"unknown", "suspended", "intraday"}:
                dates = [day for day in dates if day != "20261001"]
            if gap == "future":
                dates.append("20261003")
            return pd.DataFrame([{"ts_code": code, "trade_date": day, "open": 10, "high": 11,
                                  "low": 9, "close": 10, "vol": 1, "amount": 1, "pct_chg": 0} for day in dates])
        def get_adj_factor(self, ts_code, start_date, end_date):
            calls.append(("factor", start_date, end_date))
            dates = [day for day in ["20260930", "20261001", "20261002"] if start_date <= day <= end_date]
            if gap == "factor":
                dates = dates[:-1]
            return pd.DataFrame([{"ts_code": code, "trade_date": day, "adj_factor": 1} for day in dates])
        def get_suspend_d(self, ts_code, start_date, end_date):
            return pd.DataFrame([{"ts_code": code, "trade_date": "20261001", "suspend_type": "S",
                                  "suspend_timing": "09:30-10:00" if gap == "intraday" else None}]
                                if gap in {"suspended", "intraday"} else [],
                                columns=["ts_code", "trade_date", "suspend_type", "suspend_timing"])
    if gap in {"unknown", "intraday", "calendar", "factor", "future"}:
        with pytest.raises(ValueError):
            data_sync.fetch_cn_history(Client(), code, "20260930", "20261002")
    else:
        daily, factors, coverage = data_sync.fetch_cn_history(Client(), code, "20260930", "20261002")
        assert coverage["complete"] and coverage["actual_last_date"] == "20261002"
        assert coverage["explained_missing"] == (["20261001"] if gap == "suspended" else [])
        assert len(daily) == (2 if gap == "suspended" else 3)
        assert set(daily.trade_date).issubset(set(factors.trade_date))
        assert calls == [("daily", "20260930", "20261002"), ("factor", "20260930", "20261002")]


def test_cn_history_ranges_batch_five_years_and_bound_older_requests():
    from modules import data_sync
    from datetime import datetime, timedelta

    assert list(data_sync._cn_history_ranges("20211021", "20261021")) == [("20211021", "20261021")]
    ranges = list(data_sync._cn_history_ranges("19900101", "20261021"))
    assert ranges[0][0] == "19900101" and ranges[-1][1] == "20261021"
    for i, (first, last) in enumerate(ranges):
        begin, end = datetime.strptime(first, "%Y%m%d"), datetime.strptime(last, "%Y%m%d")
        assert 0 <= (end - begin).days < 5000
        if i:
            assert first == (datetime.strptime(ranges[i-1][1], "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
    with pytest.raises(ValueError):
        list(data_sync._cn_history_ranges("20261022", "20261021"))


@pytest.mark.parametrize("revision", ["unchanged", "tail", "factor", "overlap"])
def test_cn_publish_only_tail_is_incremental_and_identical_retry_is_noop(temp_db, monkeypatch, revision):
    import pandas as pd
    from modules import data_sync, data_freshness
    from modules.database import get_connection, init_database, market_context
    from modules.indicators import cache_builder

    code = "600519.SH"
    daily, factors = _cn_daily_source(code)
    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            data_sync.register_cn_assets(conn, [{"ts_code": code, "asset_type": "stock", "sources": ["manual"]}])
        data_sync.publish_cn_daily_candidate(data_sync.build_cn_daily_candidate(code, daily, factors),
                                             data_freshness.get_data_version(code))
        old_version = data_freshness.get_data_version(code)
        if revision == "tail":
            daily = pd.concat([daily, pd.DataFrame([{**daily.iloc[-1].to_dict(), "trade_date": "20261021"}])], ignore_index=True)
            factors.loc[factors.trade_date == "20261021", "adj_factor"] = 2
        elif revision == "factor":
            factors.loc[0, "adj_factor"] = 1.5
        elif revision == "overlap":
            daily.loc[0, "amount"] = 124
        requested = []
        real_build = cache_builder.build_indicator_candidates
        def record(bars, *, dates=None):
            requested.append(dates)
            return real_build(bars, dates=dates)
        monkeypatch.setattr(cache_builder, "build_indicator_candidates", record)
        if revision in {"tail", "unchanged"}:
            with get_connection() as conn:
                conn.execute("CREATE TRIGGER forbid_old_delete BEFORE DELETE ON indicator_cache "
                             "WHEN OLD.trade_date<'20261021' BEGIN SELECT RAISE(ABORT,'不得重写旧指标'); END")
        result = data_sync.publish_cn_daily_candidate(data_sync.build_cn_daily_candidate(code, daily, factors), old_version)
        if revision == "unchanged":
            assert result == 0 and requested == []
            assert data_freshness.get_data_version(code) == old_version
        elif revision == "tail":
            assert result == 1 and requested == [{"20261021"}]
        else:
            assert result == 2 and requested == [None]


@pytest.mark.parametrize("kind", ["same", "wrong_market", "closed"])
def test_cn_publisher_can_borrow_only_live_same_database_lease(temp_db, kind):
    from modules import data_sync, data_freshness
    from modules.database import get_connection, init_database, market_context
    from modules.market_sync_lock import MarketSyncLease, SyncBusyError

    code = "600519.SH"
    init_database("CN")
    with market_context("CN"):
        with get_connection() as conn:
            data_sync.register_cn_assets(conn, [{"ts_code": code, "asset_type": "stock", "sources": ["manual"]}])
        version = data_freshness.get_data_version(code)
    with market_context("US" if kind == "wrong_market" else "CN"):
        lease = MarketSyncLease.acquire("borrow-test")
    try:
        if kind == "closed":
            lease.__exit__()
        with market_context("CN"):
            daily, factors = _cn_daily_source(code)
            candidate = data_sync.build_cn_daily_candidate(code, daily, factors)
            if kind == "same":
                assert data_sync.publish_cn_daily_candidate(candidate, version, lease=lease) == 2
                assert not lease.handle.closed
                with pytest.raises(SyncBusyError):
                    MarketSyncLease.acquire("still-held")
            else:
                with pytest.raises(ValueError, match="锁.*身份|锁.*释放"):
                    data_sync.publish_cn_daily_candidate(candidate, version, lease=lease)
    finally:
        lease.__exit__()


def test_cn_suspend_endpoint_uses_strict_existing_client_contract(monkeypatch):
    import pandas as pd
    from modules.tushare_client import TushareClient
    from unittest.mock import Mock

    client = TushareClient.__new__(TushareClient)
    request = Mock(return_value=pd.DataFrame())
    monkeypatch.setattr(client, "_request_cn", request)
    client.get_suspend_d("600519.SH", "20261001", "20261020")
    request.assert_called_once_with("suspend_d", ts_code="600519.SH", start_date="20261001", end_date="20261020", suspend_type="S")


@pytest.mark.parametrize("defect", [None, "raw", "factor", "orphan"])
def test_cn_ready_requires_published_basis_and_rules_missing_is_not_false(temp_db, defect):
    from modules import data_sync, data_freshness
    from modules.database import get_connection, init_database, market_context

    code = "600519.SH"
    daily, factors = _cn_daily_source(code)
    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            data_sync.register_cn_assets(conn, [{"ts_code": code, "asset_type": "stock", "sources": ["manual"]}])
        data_sync.publish_cn_daily_candidate(data_sync.build_cn_daily_candidate(code, daily, factors),
                                             data_freshness.get_data_version(code))
        with get_connection() as conn:
            if defect in {"raw", "factor"}:
                table = "cn_raw_daily" if defect == "raw" else "cn_adj_factor"
                conn.execute(f"DELETE FROM {table} WHERE trade_date='20261019'")
            elif defect == "orphan":
                conn.execute("INSERT INTO indicator_cache(ts_code,trade_date) VALUES (?, '20261021')", (code,))
        status = data_freshness.get_data_status(code)
        assert status["workspace_market"] == "CN"
        assert status["stocks"][code]["ready"] == (defect is None)
        assert status["markets"]["CN"]["latest_date"] == "20261020"
        rules = status["stocks"][code]["rules"]
        assert rules["status"] == "unknown" and rules["reason"]


@pytest.mark.parametrize("fault", [None, "download", "compute", "progress"])
def test_cn_sync_download_compute_and_progress_preserve_previous_publication(temp_db, monkeypatch, fault):
    import pandas as pd
    from modules import data_sync, data_freshness
    from modules.database import get_connection, init_database, market_context
    from modules.indicators import cache_builder
    from modules.tushare_client import TushareDataError

    code = "600519.SH"
    daily, factors = _cn_daily_source(code)
    calls = []
    class Client:
        def get_trade_cal(self, **kwargs):
            dates = pd.date_range(kwargs["start_date"], kwargs["end_date"])
            return pd.DataFrame({"cal_date": dates.strftime("%Y%m%d"), "is_open": 1})
        def get_raw_daily(self, ts_code, first, last):
            calls.append((first, last))
            if fault == "download":
                raise TushareDataError("daily", "network")
            return daily[(daily.trade_date >= first) & (daily.trade_date <= last)].copy()
        def get_adj_factor(self, ts_code, first, last):
            return factors[(factors.trade_date >= first) & (factors.trade_date <= last)].copy()
    with market_context("CN"):
        init_database()
        with get_connection() as conn:
            data_sync.register_cn_assets(conn, [{"ts_code": code, "asset_type": "stock", "sources": ["manual"]}])
        data_sync.publish_cn_daily_candidate(data_sync.build_cn_daily_candidate(code, daily, factors),
                                             data_freshness.get_data_version(code))
        version = data_freshness.get_data_version(code)
        tables = ("cn_raw_daily", "cn_adj_factor", "daily_kline", "indicator_cache", "cn_daily_publication", "sync_log")
        with get_connection() as conn:
            before = {name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")] for name in tables}
        factors.loc[0, "adj_factor"] = 1.5
        if fault == "compute":
            def fail(*args, **kwargs):
                raise RuntimeError("候选计算故障")
            monkeypatch.setattr(cache_builder, "build_indicator_candidates", fail)
        progress_calls = []
        def progress(conn, candidate):
            progress_calls.append(candidate["anchor_date"])
            conn.execute("INSERT INTO sync_log(data_type,message) VALUES ('cn_progress','测试完成项')")
            if fault == "progress":
                raise RuntimeError("迁移进度故障")
        if fault:
            with pytest.raises((RuntimeError, TushareDataError)):
                data_sync.sync_cn_daily(Client(), code, "20261019", "20261020", progress_callback=progress)
            with get_connection() as conn:
                assert {name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")] for name in tables} == before
            assert data_freshness.get_data_version(code) == version
        else:
            result = data_sync.sync_cn_daily(Client(), code, "20261019", "20261020", progress_callback=progress)
            assert result["indicator_rows"] == 2 and result["actual_last_date"] == "20261020"
            assert result["coverage"]["complete"] and progress_calls == ["20261020"]
            with get_connection() as conn:
                assert conn.execute("SELECT close FROM daily_kline ORDER BY trade_date").fetchone()[0] == 78.75
            repeat = data_sync.sync_cn_daily(Client(), code, "20261019", "20261020")
            assert repeat["indicator_rows"] == 0
        assert calls[0] == ("20261019", "20261020")


# ==================== DataSyncer 新方法 ====================


def test_data_syncer_has_sync_missing_method():
    """DataSyncer 必须有 sync_missing() 方法（v2.10.0 P1-1 新增）"""
    from modules.data_sync import DataSyncer

    assert hasattr(DataSyncer, "sync_missing"), "DataSyncer.sync_missing missing"
    # 方法签名：sync_missing(self, ts_codes, days=730)
    import inspect

    sig = inspect.signature(DataSyncer.sync_missing)
    assert "ts_codes" in sig.parameters
    assert "days" in sig.parameters
    assert sig.parameters["days"].default == 730


def test_data_syncer_has_sync_daily_and_compute_method():
    """DataSyncer 必须有 sync_daily_and_compute() 方法（v2.10.0 P1-1 新增）"""
    from modules.data_sync import DataSyncer

    assert hasattr(DataSyncer, "sync_daily_and_compute"), "DataSyncer.sync_daily_and_compute missing"
    import inspect

    sig = inspect.signature(DataSyncer.sync_daily_and_compute)
    assert "ts_codes" in sig.parameters
    assert "days" in sig.parameters
    assert sig.parameters["days"].default == 730


def test_data_syncer_local_indicators_need_no_tushare_client(temp_db, monkeypatch):
    """US兼容类仅用于离线指标，不初始化或校验无关的Tushare配置。"""
    from modules import data_sync

    monkeypatch.setenv("DATA_MODE", "jnb")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TUSHARE_API_URL", raising=False)
    monkeypatch.setattr(data_sync.ts, "set_token", lambda *a: pytest.fail("离线指标不得配置Tushare"))
    monkeypatch.setattr(data_sync.ts, "pro_api", lambda *a: pytest.fail("离线指标不得初始化Tushare"))
    syncer = data_sync.DataSyncer()
    assert syncer.sync_indicator_cache("AAPL.US") == 0


@pytest.mark.parametrize("market", ["US", "CN"])
@pytest.mark.parametrize("method,args", [
    ("sync_stock_basic", ()), ("sync_daily_kline", ("600519.SH",)),
    ("sync_missing", (["600519.SH"],)), ("sync_all_daily_kline", ()),
    ("sync_daily_and_compute", ()), ("sync_stk_factor", ("600519.SH",)),
    ("sync_all_stk_factor", ()), ("ensure_daily_basic_columns", ()),
    ("sync_daily_basic", ("600519.SH",)), ("sync_all_daily_basic", ()),
    ("sync_moneyflow", ("600519.SH", "20261020")),
])
def test_legacy_source_writes_refuse_before_any_io(temp_db, monkeypatch, market, method, args):
    from modules import data_sync
    from modules.database import get_db_path, market_context

    instance = data_sync.DataSyncer.__new__(data_sync.DataSyncer)
    monkeypatch.setattr(data_sync, "get_connection", lambda *a, **k: pytest.fail("拒绝旧写入口前不得读写业务库"))
    monkeypatch.setattr(instance, "_rate_limit", lambda *a: pytest.fail("拒绝旧入口前不得调用源端"))
    with market_context(market), pytest.raises(ValueError, match="旧Tushare写入口.*禁用"):
        getattr(instance, method)(*args)
    assert not get_db_path("CN").exists()


def test_legacy_local_indicators_reject_cn_and_wrong_code(temp_db):
    from modules.data_sync import DataSyncer
    from modules.database import get_db_path, market_context

    instance = DataSyncer()
    with pytest.raises(ValueError, match="代码|工作空间"):
        instance.sync_indicator_cache("600519.SH")
    with market_context("CN"):
        with pytest.raises(ValueError, match="CN.*原子"):
            DataSyncer()
        with pytest.raises(ValueError, match="CN.*原子"):
            instance.sync_indicator_cache("600519.SH")
    assert not get_db_path("CN").exists()


# ==================== modules/report 新模块 ====================


def test_report_module_imports():
    """modules/report 必须可 import"""
    from modules import report

    assert hasattr(report, "StockAssessment")
    assert hasattr(report, "assess_watchlist")
    assert hasattr(report, "render_assessment")
    assert hasattr(report, "write_assessment")
    assert hasattr(report, "MACRO_SECTORS")


def test_assess_watchlist_empty_returns_empty_list():
    """空 ts_codes 必须返回 []"""
    from modules.report import assess_watchlist

    assert assess_watchlist([]) == []


def test_render_assessment_empty_report_has_header():
    """空评估列表必须渲染出表头（即使没数据）"""
    from modules.report import render_assessment

    out = render_assessment([])
    assert "Z哥量化评估报告" in out
    assert "生成时间" in out
    assert "股票总数: 0只" in out


def test_render_assessment_contains_three_parts():
    """报告必须包含 3 部分（个股深度 + 板块概览 + 操作建议）"""
    from modules.report import render_assessment

    out = render_assessment([])
    assert "第一部分" in out
    assert "第二部分" in out
    assert "第三部分" in out


def test_macro_sectors_has_at_least_ten_sectors():
    """MACRO_SECTORS 必须覆盖 ≥10 个板块"""
    from modules.report import MACRO_SECTORS

    assert len(MACRO_SECTORS) >= 10


# ==================== 4 个薄壳脚本 ====================

SHELL_SCRIPTS = [
    "scripts/sync_watchlist.py",
    "scripts/sync_and_compute.py",
    "scripts/batch_compute_indicators.py",
    "scripts/generate_report.py",
]


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
@pytest.mark.parametrize("market", [None, "US", "CN"])
def test_thin_shell_binds_market_and_restores_caller(script, market, monkeypatch):
    import importlib
    from modules.database import get_db_path, get_market, market_context

    module = importlib.import_module(script.removesuffix(".py").replace("/", "."))
    seen = []

    def watchlist():
        seen.append((get_market(), get_db_path()))
        return []

    monkeypatch.setattr(module, "load_watchlist", watchlist)
    monkeypatch.setattr(sys, "argv", [script] + (["--market", market] if market else []))
    with market_context("CN"):
        module.main()
        assert get_market() == "CN"
    selected = market or "US"
    assert seen == [(selected, get_db_path(selected))]


@pytest.mark.parametrize("script,mode", [
    ("scripts.sync_watchlist", "missing"),
    ("scripts.sync_and_compute", "daily_and_compute"),
    ("scripts.batch_compute_indicators", "indicators"),
])
def test_sync_shell_uses_workspace_adapter(script, mode, monkeypatch):
    import importlib
    from modules.database import get_market

    module = importlib.import_module(script)
    monkeypatch.setattr(module, "load_watchlist", lambda: ["AAPL.US"])
    calls = []

    def sync(codes, *, mode, days=730):
        calls.append((get_market(), codes, mode, days))
        return {"AAPL.US": 1}

    monkeypatch.setattr(module, "sync_workspace_stocks", sync, raising=False)
    monkeypatch.setattr(module, "DataSyncer", lambda: pytest.fail("不得初始化旧Tushare客户端"), raising=False)
    monkeypatch.setattr(sys, "argv", [script])
    module.main()
    assert calls == [("US", ["AAPL.US"], mode, 730)]


@pytest.mark.parametrize("mode", ["missing", "daily_and_compute", "indicators"])
def test_workspace_sync_rejects_unready_cn_before_writes(temp_db, mode, monkeypatch):
    from modules import data_sync
    from modules.database import get_connection, init_database, market_context

    monkeypatch.setattr(data_sync, "DataSyncer", lambda: pytest.fail("不得初始化旧Tushare客户端"))
    with market_context("CN"):
        init_database()
        with pytest.raises(ValueError, match="CN.*尚未就绪"):
            data_sync.sync_workspace_stocks(["600519.SH"], mode=mode)
        with get_connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM sync_log WHERE data_type != 'data_epoch'").fetchone()[0] == 0


@pytest.mark.parametrize("mode,fetch_codes,indicator_codes", [
    ("missing", ["AAPL.US"], []),
    ("daily_and_compute", ["AAPL.US", "00700.HK"], ["AAPL.US", "00700.HK"]),
    ("indicators", [], ["AAPL.US", "00700.HK"]),
])
def test_workspace_sync_routes_us_without_tushare(temp_db, monkeypatch, mode, fetch_codes, indicator_codes):
    from datetime import datetime
    from unittest.mock import Mock
    from api.services import sync_service
    from modules import data_sync, yahoo_sync
    from modules.database import get_connection, get_db_path, get_market, market_context

    with get_connection() as conn:
        conn.execute("INSERT INTO daily_kline(ts_code, trade_date, open, high, low, close, vol) "
                     "VALUES ('00700.HK', '20260918', 1, 1, 1, 1, 1)")
    session, fetched, calculated = Mock(), [], []
    monkeypatch.setattr(data_sync, "DataSyncer", lambda: pytest.fail("不得初始化旧Tushare客户端"))
    monkeypatch.setattr(yahoo_sync, "_default_session", lambda: session)
    monkeypatch.setattr(sync_service, "_beijing_now", lambda: datetime(2026, 9, 23, 18))

    def fetch(code, start, end, **kwargs):
        assert get_market() == "US"
        assert kwargs == {"session": session, "incremental": True}
        fetched.append((code, start, end))
        return 2

    monkeypatch.setattr(yahoo_sync, "sync_us_daily", fetch)
    monkeypatch.setattr(data_sync, "sync_indicator_cache_incremental", lambda code: calculated.append(code) or 3)
    with market_context("US", get_db_path()):
        result = data_sync.sync_workspace_stocks(["AAPL.US", "00700.HK"], days=10, mode=mode)
    assert [row[0] for row in fetched] == fetch_codes
    assert all(row[1:] == (("20260913", "20260922") if row[0].endswith(".US")
                           else ("20260919", "20260923")) for row in fetched)
    assert calculated == indicator_codes
    assert set(result) == (set(fetch_codes) if mode == "missing" else {"AAPL.US", "00700.HK"})
    if fetched:
        session.close.assert_called_once()
    else:
        session.close.assert_not_called()
    assert list((get_db_path().parent / "backups").glob("US-pre-script-*.db"))


def test_workspace_sync_covered_date_skips_network(temp_db, monkeypatch):
    from datetime import datetime
    from api.services import sync_service
    from modules import data_sync, yahoo_sync
    from modules.database import get_connection

    with get_connection() as conn:
        conn.execute("INSERT INTO daily_kline(ts_code, trade_date, open, high, low, close, vol) "
                     "VALUES ('AAPL.US', '20260922', 1, 1, 1, 1, 1)")
    monkeypatch.setattr(sync_service, "_beijing_now", lambda: datetime(2026, 9, 23, 18))
    monkeypatch.setattr(yahoo_sync, "_default_session", lambda: pytest.fail("已覆盖上界不得联网"))
    assert data_sync.sync_workspace_stocks(["AAPL.US"], mode="daily") == {"AAPL.US": 0}


def test_workspace_sync_empty_source_is_not_success(temp_db, monkeypatch):
    from unittest.mock import Mock
    from modules import data_sync, yahoo_sync
    from modules.market_sync_lock import MarketSyncLease

    session = Mock()
    monkeypatch.setattr(yahoo_sync, "_default_session", lambda: session)
    monkeypatch.setattr(yahoo_sync, "sync_us_daily", lambda *a, **k: 0)
    with pytest.raises(RuntimeError, match="未返回.*不能确认"):
        data_sync.sync_workspace_stocks(["AAPL.US"], mode="daily")
    session.close.assert_called_once()
    with MarketSyncLease.acquire("after-failure"):
        pass


@pytest.mark.parametrize("failure", ["busy", "backup"])
def test_workspace_sync_stops_before_network_on_lock_or_backup_failure(temp_db, monkeypatch, failure):
    from contextlib import nullcontext
    from api.services import sync_service
    from modules import data_sync, yahoo_sync
    from modules.market_sync_lock import MarketSyncLease, SyncBusyError

    def backup(*args, **kwargs):
        if failure == "busy":
            pytest.fail("锁失败后不得备份")
        raise OSError("backup failed")

    monkeypatch.setattr(sync_service, "backup_database", backup)
    monkeypatch.setattr(yahoo_sync, "_default_session", lambda: pytest.fail("锁或备份失败后不得联网"))
    with MarketSyncLease.acquire("busy-owner") if failure == "busy" else nullcontext():
        with pytest.raises(SyncBusyError if failure == "busy" else OSError):
            data_sync.sync_workspace_stocks(["AAPL.US"], mode="daily")
    with MarketSyncLease.acquire("after-failure"):
        pass


@pytest.mark.parametrize("market", ["US", "CN"])
def test_report_default_filename_has_market(market, monkeypatch):
    from scripts import generate_report

    paths = []
    monkeypatch.setattr(generate_report, "load_watchlist", lambda: ["AAPL.US"] if market == "US" else ["600519.SH"])
    monkeypatch.setattr(generate_report, "assess_watchlist", lambda codes: [])
    monkeypatch.setattr(generate_report, "write_assessment", lambda assessments, path: paths.append(Path(path)) or 0)
    monkeypatch.setattr(sys, "argv", ["report", "--market", market])
    generate_report.main()
    assert paths and f"stocks_assessment_{market}_" in paths[0].name


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_thin_shell_has_help(script):
    """4 个薄壳脚本必须支持 --help 退出 0"""
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / script), "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    # argparse 默认 --help 退出 0
    assert (
        result.returncode == 0
    ), f"{script} --help exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_thin_shell_under_60_lines(script):
    """每个薄壳脚本必须 < 60 行（计划目标 <50 行，留点 buffer）"""
    lines = (PROJECT_ROOT / script).read_text(encoding="utf-8").count("\n")
    assert lines < 60, f"{script}: {lines} lines (target <50)"


def test_thin_shells_total_under_250_lines():
    """4 个薄壳脚本总行数 < 250（计划目标 <500）"""
    total = sum((PROJECT_ROOT / s).read_text(encoding="utf-8").count("\n") for s in SHELL_SCRIPTS)
    assert total < 250, f"Total: {total} lines (target <500)"


# ==================== 4 个薄壳脚本不再有重复实现 ====================


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_thin_shell_no_duplicate_compute_xxx(script):
    """4 个薄壳脚本不再含 compute_ma/ema/kdj/rsi/boll/macd 重复实现"""
    src = (PROJECT_ROOT / script).read_text(encoding="utf-8")
    forbidden = [
        "def compute_ma(",
        "def compute_ema(",
        "def compute_kdj(",
        "def compute_rsi(",
        "def compute_boll(",
        "def compute_macd(",
    ]
    for f in forbidden:
        assert f not in src, f"{script} still has duplicate: {f}"


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_thin_shell_no_hardcoded_user_path(script):
    """4 个薄壳脚本不再含 /Users/chenlei 硬编码"""
    src = (PROJECT_ROOT / script).read_text(encoding="utf-8")
    assert "/Users/chenlei" not in src, f"{script} still hardcodes /Users/chenlei"


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_thin_shell_uses_stocks_json_env_or_default(script):
    """4 个薄壳脚本必须支持 STOCKS_JSON env（直接或通过 scripts._common）"""
    src = (PROJECT_ROOT / script).read_text(encoding="utf-8")
    # v2.10.0: _load_watchlist 已提取到 scripts/_common.py，
    # 薄壳脚本通过 from scripts._common import load_watchlist 间接依赖
    common_src = (PROJECT_ROOT / "scripts" / "_common.py").read_text(encoding="utf-8")
    assert "STOCKS_JSON" in src or (
        "from scripts._common import" in src and "STOCKS_JSON" in common_src
    ), f"{script} doesn't support STOCKS_JSON env (directly or via _common)"
