"""一致性快照导出只使用临时数据库，覆盖尚未 checkpoint 的 WAL 数据。"""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

import pytest


def test_export_includes_wal_and_preserves_source(tmp_path, monkeypatch):
    from scripts.export_database import export_database

    source = tmp_path / "source #1.db"
    monkeypatch.setenv("DB_PATH", str(tmp_path / "unrelated.db"))
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE daily_kline(ts_code TEXT, trade_date TEXT)")
        writer.execute("INSERT INTO daily_kline VALUES ('AAPL.US', '20260918')")
        writer.commit()
        before = source.read_bytes()
        wal = Path(str(source) + "-wal")
        wal_before = wal.read_bytes()
        report = export_database(source)
        snapshot = Path(report["path"])
        assert snapshot != source
        assert snapshot.exists()
        assert report["sha256"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
        assert report["tables"]["daily_kline"] == 1
        assert report["latest_trade_date"] == "20260918"
        with closing(sqlite3.connect(snapshot)) as exported:
            assert exported.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert exported.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0] == 1
        assert source.read_bytes() == before
        assert wal.read_bytes() == wal_before
        assert not Path(str(snapshot) + "-wal").exists()
        second = export_database(source)
        assert second["path"] != report["path"]
    assert not (tmp_path / "unrelated.db").exists()


def test_export_assigns_independent_data_epoch(temp_db, tmp_path):
    from modules.data_freshness import get_data_version
    from modules.database import get_db_path
    from scripts.export_database import export_database

    before = get_data_version()
    result = export_database(get_db_path(), tmp_path / 'export.db')
    with closing(sqlite3.connect(result['path'])) as conn:
        row = conn.execute("SELECT message FROM sync_log WHERE data_type='data_epoch' ORDER BY id DESC LIMIT 1").fetchone()
        assert row and row[0] != before.split(':')[0]
    assert get_data_version() == before


@pytest.mark.parametrize("case", ["missing", "same", "existing", "corrupt"])
def test_export_rejects_unsafe_inputs_and_preserves_files(tmp_path, case):
    from scripts.export_database import export_database

    source = tmp_path / "source.db"
    if case != "missing":
        with closing(sqlite3.connect(source)) as conn:
            conn.execute("CREATE TABLE sample(value TEXT)")
            conn.commit()
    target = tmp_path / "snapshot.db"
    if case == "same":
        target = source
    elif case == "existing":
        target.write_bytes(b"old-snapshot")
    elif case == "corrupt":
        source.write_bytes(b"not-a-database")
    before = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    with pytest.raises((OSError, ValueError, sqlite3.DatabaseError)):
        export_database(source, target)
    after = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert after == before


def test_export_failure_does_not_publish_partial_snapshot(tmp_path, monkeypatch):
    from scripts import export_database as tool

    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("CREATE TABLE sample(value TEXT)")
    target = tmp_path / "new.db"
    old = tmp_path / "old.db"
    old.write_bytes(b"old-backup")

    def fail(destination, **kwargs):
        destination.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(tool, "backup_database", fail)
    with pytest.raises(OSError):
        tool.export_database(source, target)
    assert not target.exists()
    assert old.read_bytes() == b"old-backup"


def test_export_cli_reports_hash_and_nonzero_for_missing_source(tmp_path, capsys):
    import json
    from scripts.export_database import main

    source = tmp_path / "source.db"
    assert main(["--source", str(source)]) == 1
    assert not source.exists()
    capsys.readouterr()
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("CREATE TABLE sample(value TEXT)")
    assert main(["--source", str(source)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["sha256"]) == 64
    assert report["tables"] == {"sample": 0}
