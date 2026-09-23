"""数据版本与写入事务、批次标记的契约测试。"""

import pytest


def test_revision_is_atomic_and_noop_does_not_advance(temp_db):
    from modules.database import get_connection
    from modules.data_freshness import get_data_status, record_data_change

    before = get_data_status()["version"]
    with pytest.raises(ValueError):
        with get_connection() as conn:
            record_data_change(conn, "ADI.US", "20260922")
            raise ValueError("回滚")
    assert get_data_status()["version"] == before
    with get_connection() as conn:
        record_data_change(conn, "ADI.US", "20260922")
    after = get_data_status()["version"]
    assert after != before
    assert get_data_status()["version"] == after


def test_epoch_changes_even_with_identical_rows(temp_db):
    from modules.database import get_connection
    from modules.data_freshness import get_data_status, rotate_data_epoch

    before = get_data_status()["version"]
    with get_connection() as conn:
        rotate_data_epoch(conn)
    assert get_data_status()["version"] != before


def test_status_counts_date_gaps_not_just_row_counts(db_conn):
    from modules.data_freshness import get_data_status
    from tests.conftest import generate_uptrend_klines, write_klines_to_db

    write_klines_to_db(db_conn, generate_uptrend_klines(n=2, ts_code="ADI.US"))
    db_conn.execute("INSERT INTO indicator_cache(ts_code,trade_date) VALUES ('ADI.US','20250101'),('ADI.US','20250102')")
    db_conn.commit()
    item = get_data_status()["stocks"]["ADI.US"]
    assert item["missing_indicators"] == 2
    assert item["orphan_indicators"] == 2
    assert not item["ready"]


def test_batch_finishes_on_exception_without_changing_data_version(temp_db):
    from modules.data_freshness import data_update_batch, get_data_status

    before = get_data_status()["version"]
    with pytest.raises(ValueError):
        with data_update_batch():
            assert get_data_status()["updating"]
            raise ValueError("失败")
    assert not get_data_status()["updating"]
    assert get_data_status()["version"] == before
