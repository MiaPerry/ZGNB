"""CN 初始化只使用临时库：固定快照、可恢复清单与拒绝覆盖发布。"""

import json
from pathlib import Path
import sqlite3

import pytest


def _legacy_source(path: Path):
    from modules.database import init_database, market_context, get_connection

    with market_context("CN", path):
        init_database()
        with get_connection() as conn:
            conn.executemany(
                "INSERT INTO stock_basic(ts_code,name,market,list_date) VALUES (?,?,?,?)",
                [("600519.SH", "贵州茅台", "主板", "20010827"),
                 ("000001.SZ", "平安银行", "主板", "19910403")],
            )
            conn.execute("INSERT INTO watchlist(ts_code,name,tags,notes) VALUES ('600519.SH','贵州茅台','核心','原备注')")
            conn.execute("INSERT INTO trade_records(ts_code,trade_date,action,price,quantity,amount,notes) "
                         "VALUES ('000001.SZ','20260102','BUY',10,100,1000,'原交易')")
            conn.execute("INSERT INTO tracking_pool_self(ts_code,name,add_date,notes) "
                         "VALUES ('600519.SH','贵州茅台','2026-01-01','个人跟踪')")
            conn.execute("INSERT INTO tracking_records_self(ts_code,trade_date,close,signal_type) "
                         "VALUES ('600519.SH','20260102',10,'BUY')")


def _snapshot():
    return {
        "source": "tushare.index_weight:399300.SZ",
        "trade_date": "20261016",
        "sha256": "a" * 64,
        "codes": ["600519.SH", "000001.SZ"],
        "weights": {"600519.SH": 50.0, "000001.SZ": 50.0},
    }


def _core_preflight(ok=True):
    return {
        "passed": ok,
        "daily": {"passed": ok},
        "adj_factor": {"passed": ok},
        "trade_cal": {"passed": ok},
        "stock_basic": {"passed": ok},
        "index_daily": {"passed": ok},
        "constituents": {"passed": ok},
        "history_coverage": {"passed": ok},
        "stk_limit": {"passed": False, "blocking": False, "reason": "辅助权限不可用"},
    }


@pytest.mark.parametrize("fault", [None, "calendar", "rate_limit", "gap"])
def test_free_cn_inputs_probe_once_and_do_not_claim_all_history(monkeypatch, fault):
    import pandas as pd
    from modules import cn_initializer
    from modules.tushare_client import TushareDataError
    from tests.test_data_sync_extensions import _cn_daily_source

    calls = []
    daily, factors = _cn_daily_source()
    if fault == "gap":
        daily = daily.iloc[:1]
    class Client:
        def get_trade_cal(self, **kw):
            calls.append("trade_cal")
            dates = ["20261019"] if fault == "calendar" else ["20261019", "20261020"]
            return pd.DataFrame({"cal_date": dates, "is_open": 1})
        def get_raw_daily(self, *args):
            calls.append("daily")
            return daily
        def get_adj_factor(self, *args):
            calls.append("adj_factor")
            if fault == "rate_limit":
                raise TushareDataError("adj_factor", "rate_limit")
            return factors.iloc[:2]
        def get_index_daily(self, *args, **kw):
            calls.append("index_daily")
            return _cn_daily_source("000300.SH")[0]
        def get_stock_basic(self, **kw):
            calls.append("stock_basic")
            return pd.DataFrame([{"ts_code": "600519.SH", "name": "贵州茅台", "market": "主板", "list_date": "20010827"}])
    monkeypatch.setattr(cn_initializer, "parse_csindex_closeweight", lambda *a: _snapshot())
    result = cn_initializer.build_cn_input_report(Client(), b"xls", "20261019", "20261020")
    report = result["preflight"]
    assert report["passed"] == (fault is None)
    assert report["history_coverage"]["scope"] == "sample"
    assert report["history_coverage"]["all_assets_verified"] is False
    assert sorted(calls) == ["adj_factor", "daily", "index_daily", "stock_basic", "trade_cal"]
    assert "index_weight" not in report
    if fault == "rate_limit":
        assert report["adj_factor"]["kind"] == "rate_limit"
    if fault == "calendar":
        assert not report["constituents"]["passed"]
        assert result["snapshot"] is None


def test_free_cn_input_cli_writes_failure_report_and_refuses_overwrite(tmp_path, monkeypatch, capsys):
    from scripts import build_cn_inputs
    from unittest.mock import Mock

    output = tmp_path / ".cn-init-inputs-test"
    monkeypatch.setattr(build_cn_inputs, "download_csindex_closeweight", lambda: b"xls")
    monkeypatch.setattr(build_cn_inputs, "TushareClient", lambda: object())
    probe = Mock(return_value={"snapshot": None, "preflight": {"passed": False}})
    monkeypatch.setattr(build_cn_inputs, "build_cn_input_report", probe)
    arguments = ["--output", str(output), "--cutoff", "20261021"]
    assert build_cn_inputs.main(arguments) == 1
    assert json.loads((output / "preflight.json").read_text(encoding="utf-8")) == {"passed": False}
    assert not (output / "snapshot.json").exists()
    before = (output / "preflight.json").read_bytes()
    with pytest.raises(FileExistsError):
        build_cn_inputs.main(arguments)
    assert (output / "preflight.json").read_bytes() == before
    assert probe.call_count == 1
    capsys.readouterr()


def test_cn_init_cli_dry_run_and_five_year_boundary_are_read_only(tmp_path, capsys):
    from scripts import init_cn_database

    source = tmp_path / "legacy.db"
    target = tmp_path / "stock_data_cn.db"
    snapshot = tmp_path / "snapshot.json"
    _legacy_source(source)
    snapshot.write_text(json.dumps(_snapshot()), encoding="utf-8")
    assert init_cn_database.main(["--source", str(source), "--target", str(target),
                                  "--snapshot", str(snapshot), "--cutoff", "20261020", "--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report["target"] == str(target) and not target.exists()
    assert init_cn_database._five_year_start("20240229") == "20190228"


def test_dry_run_is_read_only_and_reports_fixed_identity_space_and_codes(tmp_path):
    from modules.cn_initializer import inspect_cn_initialization

    source = tmp_path / "legacy.db"
    target = tmp_path / "stock_data_cn.db"
    _legacy_source(source)
    before = source.read_bytes()
    report = inspect_cn_initialization(source, target, _snapshot(), "20261020")
    assert source.read_bytes() == before and not target.exists()
    assert report["source"]["sha256"] and report["source"]["tables"]["watchlist"] == 1
    assert report["source"]["codes"] == ["000001.SZ", "600519.SH"]
    assert report["constituents"]["sha256"] == "a" * 64
    assert report["required_bytes"] > report["source"]["size_bytes"]
    assert report["available_bytes"] > 0
    assert report["writable"] and report["same_volume"]
    assert list(tmp_path.glob(".cn-init-*")) == []


def test_start_fixes_source_snapshot_and_migrates_personal_rows_idempotently(tmp_path):
    from modules.cn_initializer import CNInitializer
    from modules.database import get_connection, market_context

    source = tmp_path / "legacy.db"
    target = tmp_path / "stock_data_cn.db"
    _legacy_source(source)
    initializer = CNInitializer(source, target, _snapshot(), "20261020", core_preflight=_core_preflight())
    state = initializer.start()
    assert state["status"] == "staging" and state["task_id"]
    assert Path(state["source_snapshot"]).is_file() and Path(state["candidate_db"]).is_file()
    assert state["fingerprint"]
    manifest = json.loads(Path(state["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["source_summary"]["sha256"] == manifest["source_snapshot_sha256"]
    assert manifest["source_summary"]["tables"]["watchlist"] == 1
    assert manifest["source_summary"]["codes"] == ["000001.SZ", "600519.SH"]
    assert manifest["tables"]["watchlist"]["status"] == "complete"
    assert manifest["tables"]["trade_records"]["status"] == "complete"
    assert manifest["historical_only"]["tracking_records_self"] == 1
    with market_context("CN", state["candidate_db"]), get_connection() as conn:
        assert tuple(conn.execute("SELECT tags,notes FROM watchlist").fetchone()) == ("核心", "原备注")
        assert tuple(conn.execute("SELECT price,quantity,notes FROM trade_records").fetchone()) == (10, 100, "原交易")
        assert conn.execute("SELECT notes FROM tracking_pool_self").fetchone()[0] == "个人跟踪"
        assert conn.execute("SELECT COUNT(*) FROM tracking_records_self").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cn_asset_metadata").fetchone()[0] == 3
    resumed = CNInitializer.resume(state["manifest_path"], source=source, target=target,
                                   constituent_snapshot=_snapshot(), cutoff_date="20261020",
                                   core_preflight=_core_preflight()).start()
    assert resumed["task_id"] == state["task_id"]
    with market_context("CN", state["candidate_db"]), get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] == 1


def test_resume_rejects_fingerprint_change_and_completed_table_tampering(tmp_path):
    from modules.cn_initializer import CNInitializer, ResumeMismatchError

    source = tmp_path / "legacy.db"
    target = tmp_path / "stock_data_cn.db"
    _legacy_source(source)
    state = CNInitializer(source, target, _snapshot(), "20261020", core_preflight=_core_preflight()).start()
    changed = {**_snapshot(), "sha256": "b" * 64}
    with pytest.raises(ResumeMismatchError, match="指纹"):
        CNInitializer.resume(state["manifest_path"], source=source, target=target,
                             constituent_snapshot=changed, cutoff_date="20261020",
                             core_preflight=_core_preflight())
    changed_preflight = _core_preflight()
    changed_preflight["daily"]["verified_at"] = "2026-10-21T00:00:00Z"
    with pytest.raises(ResumeMismatchError, match="指纹"):
        CNInitializer.resume(state["manifest_path"], source=source, target=target,
                             constituent_snapshot=_snapshot(), cutoff_date="20261020",
                             core_preflight=changed_preflight)
    with sqlite3.connect(state["candidate_db"]) as conn:
        conn.execute("UPDATE watchlist SET notes='被篡改'")
    with pytest.raises(ResumeMismatchError, match="已完成项"):
        CNInitializer.resume(state["manifest_path"], source=source, target=target,
                             constituent_snapshot=_snapshot(), cutoff_date="20261020",
                             core_preflight=_core_preflight()).start()


@pytest.mark.parametrize("fault", ["stock", "publish"])
def test_per_stock_resume_and_no_partial_target(tmp_path, fault):
    from modules.cn_initializer import CNInitializer
    from modules import data_sync, data_freshness
    from modules.database import get_connection, market_context
    from tests.test_data_sync_extensions import _cn_daily_source

    source = tmp_path / "legacy.db"
    target = tmp_path / "stock_data_cn.db"
    _legacy_source(source)
    calls = []
    def initialize_stock(code, cutoff, progress):
        calls.append(code)
        if fault == "stock" and code == "600519.SH" and calls.count(code) == 1:
            raise RuntimeError("模拟中断")
        daily, factors = _cn_daily_source(code)
        with get_connection() as conn:
            row = conn.execute("SELECT asset_type FROM cn_asset_metadata WHERE ts_code=?", (code,)).fetchone()
        candidate = data_sync.build_cn_daily_candidate(code, daily, factors if row[0] == "stock" else None,
                                                       asset_type=row[0])
        data_sync.publish_cn_daily_candidate(candidate, data_freshness.get_data_version(code),
                                             progress_callback=progress)
    initializer = CNInitializer(source, target, _snapshot(), "20261020", core_preflight=_core_preflight(),
                                stock_initializer=initialize_stock)
    if fault == "stock":
        with pytest.raises(RuntimeError, match="模拟中断"):
            initializer.run()
        assert not target.exists()
        initializer = CNInitializer.resume(initializer.manifest_path, source=source, target=target,
                                            constituent_snapshot=_snapshot(), cutoff_date="20261020",
                                            core_preflight=_core_preflight(), stock_initializer=initialize_stock)
        result = initializer.run()
        assert calls.count("000001.SZ") == 1
    else:
        initializer.start()
        target.write_bytes(b"existing")
        with pytest.raises(FileExistsError, match="禁止覆盖"):
            initializer.run()
        assert target.read_bytes() == b"existing"
        return
    assert result["status"] == "published" and target.is_file()
    assert not Path(result["candidate_db"]).exists()
    with market_context("CN", target), get_connection() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cn_daily_publication").fetchone()[0] == 3


def test_resume_confirms_matching_target_when_publish_manifest_lagged(tmp_path, monkeypatch):
    import os
    from modules.cn_initializer import CNInitializer
    from modules import data_sync, data_freshness
    from modules.database import get_connection
    from tests.test_data_sync_extensions import _cn_daily_source

    source = tmp_path / "legacy.db"
    target = tmp_path / "stock_data_cn.db"
    _legacy_source(source)
    def initialize_stock(code, cutoff, progress):
        daily, factors = _cn_daily_source(code)
        with get_connection() as conn:
            asset_type = conn.execute("SELECT asset_type FROM cn_asset_metadata WHERE ts_code=?", (code,)).fetchone()[0]
        candidate = data_sync.build_cn_daily_candidate(code, daily, factors if asset_type == "stock" else None,
                                                       asset_type=asset_type)
        data_sync.publish_cn_daily_candidate(candidate, data_freshness.get_data_version(code),
                                             progress_callback=progress)
    initializer = CNInitializer(source, target, _snapshot(), "20261020", core_preflight=_core_preflight(),
                                stock_initializer=initialize_stock)
    def lagged_publish():
        candidate = Path(initializer._manifest["candidate_db"])
        os.link(candidate, target)
        candidate.unlink()
    monkeypatch.setattr(initializer, "_publish", lagged_publish)
    state = initializer.run()
    assert state["status"] == "staging" and target.exists()
    resumed = CNInitializer.resume(state["manifest_path"], source=source, target=target,
                                   constituent_snapshot=_snapshot(), cutoff_date="20261020",
                                   core_preflight=_core_preflight()).start()
    assert resumed["status"] == "published" and resumed["target_sha256"]


def test_initialization_refuses_existing_target_failed_core_space_and_cross_volume(tmp_path, monkeypatch):
    import os
    import shutil
    from modules.cn_initializer import CNInitializer, InitializationPreflightError

    source = tmp_path / "legacy.db"
    target = tmp_path / "stock_data_cn.db"
    _legacy_source(source)
    target.write_bytes(b"existing")
    with pytest.raises(FileExistsError, match="禁止覆盖"):
        CNInitializer(source, target, _snapshot(), "20261020", core_preflight=_core_preflight()).start()
    target.unlink()
    with pytest.raises(InitializationPreflightError, match="核心"):
        CNInitializer(source, target, _snapshot(), "20261020", core_preflight=_core_preflight(False)).start()
    partial = _core_preflight()
    partial["adj_factor"]["passed"] = False
    with pytest.raises(InitializationPreflightError, match="核心"):
        CNInitializer(source, target, _snapshot(), "20261020", core_preflight=partial).start()
    real_usage = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type("U", (), {"free": 1})())
    with pytest.raises(InitializationPreflightError, match="空间"):
        CNInitializer(source, target, _snapshot(), "20261020", core_preflight=_core_preflight()).start()
    monkeypatch.setattr(shutil, "disk_usage", real_usage)
    real_stat = os.stat
    class DifferentDevice:
        def __init__(self, stat):
            self.st_dev = stat.st_dev + 1
            self.__stat = stat
        def __getattr__(self, name):
            return getattr(self.__stat, name)
    monkeypatch.setattr(os, "stat", lambda path, *a, **kw: DifferentDevice(real_stat(path, *a, **kw))
                        if Path(path) == source else real_stat(path, *a, **kw))
    with pytest.raises(InitializationPreflightError, match="同卷"):
        CNInitializer(source, target, _snapshot(), "20261020", core_preflight=_core_preflight()).start()
