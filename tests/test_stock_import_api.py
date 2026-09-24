"""股票收录接口测试：使用真实服务与隔离库，不请求外网。"""

import pytest
from fastapi.testclient import TestClient
from tests.test_stock_import import import_service  # noqa: F401


@pytest.fixture
def client(import_service, monkeypatch):
    from api.main import app
    from api.routes import stock

    monkeypatch.setattr(stock, "get_stock_import_service", lambda: import_service)
    with TestClient(app) as instance:
        yield instance


def test_import_api_and_persistent_progress(client, import_service):
    assert client.get("/api/v1/stock/imports/latest").json()["status"] == "idle"
    result = client.post("/api/v1/stock/imports", json={"codes": "WULF,IREN"})
    assert result.status_code == 202
    task_id = result.json()["task_id"]
    sequence, revision = result.json()["sequence"], result.json()["revision"]
    assert sequence > 0
    assert import_service.wait_for_completion(30)
    result = client.get(f"/api/v1/stock/imports/{task_id}")
    assert result.status_code == 200
    assert result.json()["success"] == 2
    assert result.json()["sequence"] == sequence
    assert result.json()["revision"] > revision
    assert "owned" not in result.json()["items"][0]
    assert client.get("/api/v1/stock/imports/latest").json()["task_id"] == task_id
    stocks = client.get("/api/v1/stock/list?market=美股").json()
    assert stocks["total"] == 2
    assert all(row["indicators_ready"] for row in stocks["items"])
    assert client.post(f"/api/v1/stock/imports/{task_id}/retry").status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"codes": ""},
        {"codes": ["WULF"]},
        {"codes": "00700.HK"},
        {"codes": "A" * 5001},
        {"codes": "WULF", "overwrite": True},
    ],
)
def test_bad_input_422(client, payload):
    assert client.post("/api/v1/stock/imports", json=payload).status_code == 422


def test_batch_limit_422(client):
    payload = {"codes": " ".join(f"S{i}" for i in range(51))}
    result = client.post("/api/v1/stock/imports", json=payload)
    assert result.status_code == 422
    assert "最多添加 50 只" in result.json()["detail"]


def test_unknown_task_404(client):
    assert client.get("/api/v1/stock/imports/unknown").status_code == 404
    assert client.post("/api/v1/stock/imports/unknown/retry").status_code == 404


def test_all_web_sync_entrypoints_respect_lock(client):
    from modules.market_sync_lock import MarketSyncLease

    with MarketSyncLease.acquire("test-holder"):
        assert client.post("/api/v1/stock/imports", json={"codes": "WULF"}).status_code == 409
        assert client.post("/api/v1/system/sync/batch").status_code == 409
        assert client.post("/api/v1/system/sync/WULF.US").status_code == 409


def test_list_distinguishes_pending_indicators(client, import_service):
    def fail(_):
        raise RuntimeError("测试错误")

    import_service.compute = fail
    client.post("/api/v1/stock/imports", json={"codes": "WULF"})
    assert import_service.wait_for_completion(30)
    stock = client.get("/api/v1/stock/list?market=美股").json()["items"][0]
    assert stock["data_status"] == "available"
    assert not stock["indicators_ready"]
