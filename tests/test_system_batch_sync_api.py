"""批量同步 HTTP 接口测试：快速返回、防重入、状态查询、无 token 可用、既有接口兼容"""

import threading

import pytest
from fastapi.testclient import TestClient

from tests.conftest import write_klines_to_db


@pytest.fixture
def api_env(temp_db, db_conn, tmp_path, monkeypatch):
    """构造使用临时库与假同步器的 API 环境"""
    from api.services.sync_service import USStockSyncService
    from tests.test_us_sync_service import FakeSyncer, FIXED_NOW

    syncer = FakeSyncer()
    service = USStockSyncService(
        syncer=syncer,
        sleep=lambda s: None,
        now_provider=lambda: FIXED_NOW,
        backup_dir=tmp_path / "backups",
    )

    from api.routes import system as system_module

    monkeypatch.setattr(system_module, "get_a_share_sync_service", lambda: service)

    from api.main import app

    client = TestClient(app)
    return client, service, syncer, db_conn


def test_status_idle_before_any_task(api_env):
    """无任务时返回空闲状态"""
    client, *_ = api_env
    resp = client.get("/api/v1/system/sync/batch/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "idle"
    assert body["task_id"] is None


def test_submit_returns_snapshot_quickly_and_completes(api_env):
    """提交立即返回任务快照，后台完成后可查询终态"""
    client, service, syncer, db_conn = api_env
    write_klines_to_db(
        db_conn,
        [{"ts_code": "AAPL.US", "trade_date": "20260917", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0}],
    )
    syncer._outcomes = {"AAPL.US": [2]}

    resp = client.post("/api/v1/system/sync/batch")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["task_id"]

    assert service.wait_for_completion(timeout=10)
    final = client.get("/api/v1/system/sync/batch/status").json()
    assert final["status"] == "completed"
    assert final["success"] == 1
    assert final["new_rows"] == 2


def test_duplicate_submit_returns_same_task(api_env):
    """运行中重复提交返回同一 task_id，不产生第二个任务"""
    client, service, syncer, db_conn = api_env
    write_klines_to_db(
        db_conn,
        [{"ts_code": "AAPL.US", "trade_date": "20260917", "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0}],
    )
    gate = threading.Event()
    syncer._gate = gate
    syncer._outcomes = {"AAPL.US": [1]}

    first = client.post("/api/v1/system/sync/batch").json()
    second = client.post("/api/v1/system/sync/batch").json()
    gate.set()
    assert service.wait_for_completion(timeout=10)

    assert first["task_id"] == second["task_id"]
    assert len(syncer.daily_calls) == 1


def test_submit_works_without_tushare_token(temp_db, db_conn, tmp_path, monkeypatch):
    """美股/港股走 Yahoo，缺少 TUSHARE_TOKEN 时接口仍可正常提交（不返回 503）"""
    import os

    from api.services.sync_service import USStockSyncService
    from tests.test_us_sync_service import FakeSyncer, FIXED_NOW
    from api.routes import system as system_module
    from api.main import app

    os.environ.pop("TUSHARE_TOKEN", None)
    service = USStockSyncService(
        syncer=FakeSyncer(),
        sleep=lambda s: None,
        now_provider=lambda: FIXED_NOW,
        backup_dir=tmp_path / "backups",
    )
    monkeypatch.setattr(system_module, "get_a_share_sync_service", lambda: service)

    client = TestClient(app)
    resp = client.post("/api/v1/system/sync/batch")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    assert service.wait_for_completion(timeout=10)


def test_data_status_is_read_only_and_reports_gaps(api_env):
    client, _, _, conn = api_env
    write_klines_to_db(conn, [{"ts_code": "AAPL.US", "trade_date": "20260918", "open": 1,
                             "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1, "pct_chg": 0}])
    before = conn.execute('SELECT COUNT(*) FROM sync_log').fetchone()[0]
    response = client.get('/api/v1/system/data/status')
    assert response.status_code == 200
    body = response.json()
    assert body['stocks']['AAPL.US']['missing_indicators'] == 1
    assert not body['ready']
    assert conn.execute('SELECT COUNT(*) FROM sync_log').fetchone()[0] == before


def test_existing_sync_log_endpoint_unchanged(api_env):
    """既有 /sync/status 日志接口返回结构保持不变"""
    client, *_ = api_env
    resp = client.get("/api/v1/system/sync/status")
    assert resp.status_code == 200
    assert "logs" in resp.json()
