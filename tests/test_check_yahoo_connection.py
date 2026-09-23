"""Yahoo 检查工具只读验收，使用模拟响应且禁止 SQLite 连接。"""

from datetime import datetime, timezone
import sqlite3
from unittest.mock import Mock

import pytest

from tests.test_yahoo_sync import FakeResponse, FakeSession, _chart_payload


@pytest.mark.parametrize("outcome, expected", [("valid", 0), ("empty", 2), ("error", 1), ("invalid", 2)])
def test_connection_check_reports_bars_without_database(tmp_path, monkeypatch, capsys, outcome, expected):
    from scripts import check_yahoo_connection as tool

    database = tmp_path / "not-created" / "test.db"
    monkeypatch.setenv("DB_PATH", str(database))
    connect = Mock(side_effect=AssertionError("检查工具不得连接数据库"))
    monkeypatch.setattr(sqlite3, "connect", connect)
    timestamp = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    payload = _chart_payload(
        [timestamp], {"open": [100], "high": [102], "low": [99], "close": [101], "volume": [1000]}, 0
    )
    if outcome == "empty":
        payload = {"chart": {"result": []}}
    if outcome == "invalid":
        payload["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [float("nan")]
    response = (
        ConnectionError("proxy http://test-user:test-secret@invalid") if outcome == "error" else FakeResponse(payload)
    )
    session = FakeSession([response])
    session.close = Mock()
    monkeypatch.setattr(tool.yahoo, "_default_session", lambda: session)

    assert tool.main([]) == expected
    output = capsys.readouterr()
    assert "test-secret" not in output.out + output.err
    if outcome == "valid":
        assert "AAPL" in output.out
        assert datetime.now(timezone.utc).strftime("%Y%m%d") in output.out
        assert '"count": 1' in output.out
    connect.assert_not_called()
    session.close.assert_called_once()
    assert not database.parent.exists()
