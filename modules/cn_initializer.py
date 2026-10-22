"""CN 候选库初始化：固定源快照、可恢复清单和拒绝覆盖发布。"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from api.services.sync_service import backup_database
from .database import get_connection, init_database, market_context
from .data_freshness import rotate_data_epoch
from .data_sync import (
    CN_INDEX_BASIS_VERSION,
    CN_PRICE_BASIS_VERSION,
    fetch_cn_history,
    parse_csindex_closeweight,
    register_cn_assets,
)


class InitializationPreflightError(RuntimeError):
    """核心能力、空间、权限或同卷发布门槛未通过。"""


class ResumeMismatchError(RuntimeError):
    """续作身份或已完成数据不再匹配。"""


class MigrationConflictError(RuntimeError):
    """同一源记录与候选库记录有差异，禁止静默覆盖。"""


def build_cn_input_report(client, content: bytes, start_date: str, cutoff: str,
                          *, sample_code: str = "600519.SH") -> dict:
    """核心接口各请求一次，复用响应验证抽样历史；全量发布仍需逐股核验。"""
    import pandas as pd
    from types import SimpleNamespace
    from .tushare_client import TushareDataError
    from .database import validate_market_code

    validate_market_code(sample_code, "CN")
    expected_days = set(pd.date_range(start_date, cutoff).strftime("%Y%m%d"))
    if not expected_days:
        raise ValueError("CN预检起止日期无效")
    frames, report = {}, {}
    probes = {
        "trade_cal": lambda: client.get_trade_cal(exchange="SSE", start_date=start_date,
                                                 end_date=cutoff, raise_on_error=True),
        "daily": lambda: client.get_raw_daily(sample_code, start_date, cutoff),
        "adj_factor": lambda: client.get_adj_factor(sample_code, start_date, cutoff),
        "index_daily": lambda: client.get_index_daily("000300.SH", start_date, cutoff, raise_on_error=True),
        "stock_basic": lambda: client.get_stock_basic(ts_code=sample_code, raise_on_error=True),
    }
    for endpoint, probe in probes.items():
        try:
            frame = probe()
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                raise ValueError("接口未返回有效数据")
            if endpoint == "trade_cal" and (
                    not {"cal_date", "is_open"}.issubset(frame.columns)
                    or len(frame) != len(expected_days) or set(frame.cal_date) != expected_days
                    or not frame.is_open.isin([0, 1]).all()):
                raise ValueError("交易日历截断或结构无效")
            if endpoint == "stock_basic" and (
                    not {"ts_code", "name", "market", "list_date"}.issubset(frame.columns)
                    or len(frame) != 1 or frame.iloc[0].ts_code != sample_code
                    or any(pd.isna(frame.iloc[0][key]) or not frame.iloc[0][key]
                           for key in ("name", "market", "list_date"))):
                raise ValueError("抽样股票基本资料不完整")
            frames[endpoint] = frame
            report[endpoint] = {"passed": True, "rows": len(frame)}
        except (TushareDataError, ValueError) as exc:
            report[endpoint] = {"passed": False, "kind": getattr(exc, "kind", "invalid_response"),
                                "reason": str(exc)}
    snapshot = None
    try:
        if "trade_cal" not in frames:
            raise ValueError("交易日历不可用，无法核验成分快照日期")
        calendar = frames["trade_cal"]
        snapshot = parse_csindex_closeweight(content, cutoff, set(calendar.loc[calendar.is_open == 1, "cal_date"]))
        report["constituents"] = {"passed": True, "source": snapshot["source"],
                                  "trade_date": snapshot["trade_date"], "sha256": snapshot["sha256"]}
    except ValueError as exc:
        report["constituents"] = {"passed": False, "reason": str(exc)}

    def sliced(endpoint, first, last):
        frame = frames[endpoint]
        column = "cal_date" if endpoint == "trade_cal" else "trade_date"
        return frame.loc[frame[column].between(first, last)].copy()

    def no_suspension(*args):
        raise ValueError("免费源无法核实停牌缺口，禁止将空行情标为完整")

    coverage = {"passed": False, "scope": "sample", "sample_code": sample_code, "all_assets_verified": False}
    try:
        if not all(report[key]["passed"] for key in probes):
            raise ValueError("核心接口未通过，不能核验抽样历史")
        cached = SimpleNamespace(
            get_trade_cal=lambda **kw: sliced("trade_cal", kw["start_date"], kw["end_date"]),
            get_raw_daily=lambda code, first, last: sliced("daily", first, last),
            get_adj_factor=lambda code, first, last: sliced("adj_factor", first, last),
            get_index_daily=lambda code, first, last, **kw: sliced("index_daily", first, last),
            get_suspend_d=no_suspension,
        )
        metadata = frames["stock_basic"].iloc[0].to_dict()
        _, _, stock_coverage = fetch_cn_history(cached, sample_code, start_date, cutoff, metadata=metadata)
        _, _, index_coverage = fetch_cn_history(cached, "000300.SH", start_date, cutoff, asset_type="index")
        if not stock_coverage["rows"] or not index_coverage["rows"]:
            raise ValueError("抽样历史没有有效行情")
        coverage.update(passed=True, stock=stock_coverage, index=index_coverage)
    except (ValueError, KeyError) as exc:
        coverage["reason"] = str(exc) if isinstance(exc, ValueError) else "历史响应结构无效"
    report["history_coverage"] = coverage
    report["passed"] = all(value["passed"] for value in report.values())
    report.update(start_date=start_date, cutoff_date=cutoff)
    return {"snapshot": snapshot, "preflight": report}


_SCHEMA_VERSION = "cn-init-v1"
_PERSONAL_TABLES = ("watchlist", "trade_records", "tracking_pool_self")
_HISTORICAL_TABLES = ("tracking_records_self", "monthly_reviews_self", "strategy_performance_self")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_names(conn) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def _rows_digest(conn, table: str) -> tuple[int, str]:
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({_quoted(table)})")]
    if not columns:
        return 0, _json_hash([])
    order = "id" if "id" in columns else ",".join(_quoted(column) for column in columns)
    rows = [list(row) for row in conn.execute(f"SELECT * FROM {_quoted(table)} ORDER BY {order}")]
    return len(rows), _json_hash(rows)


def _logical_source_report(source: Path) -> dict:
    uri = source.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        tables = {}
        logical = []
        for table in sorted(_table_names(conn)):
            count, digest = _rows_digest(conn, table)
            tables[table] = count
            logical.append((table, count, digest))
        codes = set()
        for table, column in (("stock_basic", "ts_code"), ("daily_kline", "ts_code"),
                              ("watchlist", "ts_code"), ("trade_records", "ts_code"),
                              ("tracking_pool_self", "ts_code")):
            if table in tables:
                codes.update(row[0] for row in conn.execute(
                    f"SELECT DISTINCT {_quoted(column)} FROM {_quoted(table)} WHERE {_quoted(column)} IS NOT NULL"
                ))
    cn_codes = sorted(code for code in codes if isinstance(code, str) and
                      (code.endswith(".SH") or code.endswith(".SZ")))
    files = [source, Path(str(source) + "-wal"), Path(str(source) + "-shm")]
    size = sum(path.stat().st_size for path in files if path.exists())
    return {"path": str(source), "sha256": _json_hash(logical), "tables": tables,
            "codes": cn_codes, "size_bytes": size}


def _validate_snapshot(snapshot: dict) -> dict:
    required = {"source", "trade_date", "sha256", "codes"}
    if not isinstance(snapshot, dict) or not required.issubset(snapshot):
        raise InitializationPreflightError("沪深300名单快照结构无效")
    codes = list(snapshot["codes"])
    if len(codes) != len(set(codes)) or any(
            not isinstance(code, str) or not (code.endswith(".SH") or code.endswith(".SZ")) for code in codes):
        raise InitializationPreflightError("沪深300名单快照代码无效")
    return {**snapshot, "codes": sorted(codes)}


def inspect_cn_initialization(source, target, constituent_snapshot, cutoff_date) -> dict:
    """只读预检，不创建快照、staging或目标库。"""
    source, target = Path(source).resolve(), Path(target).resolve()
    if not source.is_file():
        raise FileNotFoundError("CN初始化源数据库不存在")
    datetime.strptime(cutoff_date, "%Y%m%d")
    snapshot = _validate_snapshot(constituent_snapshot)
    target_parent = target.parent
    if not target_parent.is_dir():
        raise InitializationPreflightError("CN目标目录不存在")
    source_report = _logical_source_report(source)
    required = max(source_report["size_bytes"] * 4, 4 * 1024 * 1024)
    available = shutil.disk_usage(target_parent).free
    try:
        same_volume = os.stat(source).st_dev == os.stat(target_parent).st_dev
    except OSError:
        same_volume = False
    return {
        "source": source_report,
        "target": str(target),
        "constituents": snapshot,
        "cutoff_date": cutoff_date,
        "required_bytes": required,
        "available_bytes": available,
        "writable": os.access(target_parent, os.W_OK),
        "same_volume": same_volume,
    }


def _atomic_manifest(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _copy_table(source, target, table: str) -> dict:
    source_tables, target_tables = _table_names(source), _table_names(target)
    if table not in source_tables or table not in target_tables:
        return {"status": "complete", "rows": 0, "sha256": _json_hash([])}
    source_columns = [row[1] for row in source.execute(f"PRAGMA table_info({_quoted(table)})")]
    target_columns = {row[1] for row in target.execute(f"PRAGMA table_info({_quoted(table)})")}
    columns = [column for column in source_columns if column in target_columns]
    if not columns:
        return {"status": "complete", "rows": 0, "sha256": _json_hash([])}
    select = f"SELECT {','.join(_quoted(column) for column in columns)} FROM {_quoted(table)}"
    rows = [tuple(row) for row in source.execute(select)]
    placeholders = ",".join("?" for _ in columns)
    insert = f"INSERT INTO {_quoted(table)}({','.join(_quoted(column) for column in columns)}) VALUES ({placeholders})"
    pk = [row[1] for row in target.execute(f"PRAGMA table_info({_quoted(table)})") if row[5]]
    for row in rows:
        if pk:
            identity = {key: row[columns.index(key)] for key in pk}
            where = " AND ".join(f"{_quoted(key)}=?" for key in pk)
            existing = target.execute(f"SELECT {','.join(_quoted(column) for column in columns)} "
                                      f"FROM {_quoted(table)} WHERE {where}", tuple(identity.values())).fetchone()
            if existing is not None:
                if tuple(existing) != row:
                    raise MigrationConflictError(f"{table}源记录与候选库记录冲突")
                continue
        try:
            target.execute(insert, row)
        except sqlite3.IntegrityError as exc:
            raise MigrationConflictError(f"{table}迁移记录冲突") from exc
    count, digest = _rows_digest(target, table)
    return {"status": "complete", "rows": count, "sha256": digest}


class CNInitializer:
    def __init__(self, source, target, constituent_snapshot, cutoff_date, *, core_preflight,
                 stock_initializer=None, task_id=None, manifest_path=None):
        self.source = Path(source).resolve()
        self.target = Path(target).resolve()
        self.snapshot = _validate_snapshot(constituent_snapshot)
        self.cutoff_date = cutoff_date
        datetime.strptime(cutoff_date, "%Y%m%d")
        self.core_preflight = core_preflight
        self.stock_initializer = stock_initializer
        self.task_id = task_id or uuid.uuid4().hex
        self.staging_dir = self.target.parent / f".cn-init-{self.task_id}"
        self.manifest_path = Path(manifest_path).resolve() if manifest_path else self.staging_dir / "manifest.json"
        self._manifest = None

    @classmethod
    def resume(cls, manifest_path, *, source, target, constituent_snapshot, cutoff_date,
               core_preflight, stock_initializer=None):
        path = Path(manifest_path).resolve()
        if not path.is_file():
            raise FileNotFoundError("CN初始化续作清单不存在")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        requested = {
            "source": str(Path(source).resolve()), "target": str(Path(target).resolve()),
            "constituent_sha256": constituent_snapshot.get("sha256"), "cutoff_date": cutoff_date,
            "schema_version": _SCHEMA_VERSION, "core_preflight_sha256": _json_hash(core_preflight),
        }
        if any(manifest.get(key) != value for key, value in requested.items()):
            raise ResumeMismatchError("CN初始化续作指纹不一致")
        instance = cls(source, target, constituent_snapshot, cutoff_date, core_preflight=core_preflight,
                       stock_initializer=stock_initializer, task_id=manifest["task_id"], manifest_path=path)
        instance._manifest = manifest
        return instance

    def _identity(self, source_snapshot_sha: str) -> dict:
        return {
            "source": str(self.source), "target": str(self.target),
            "constituent_sha256": self.snapshot["sha256"], "cutoff_date": self.cutoff_date,
            "schema_version": _SCHEMA_VERSION, "source_snapshot_sha256": source_snapshot_sha,
            "core_preflight_sha256": _json_hash(self.core_preflight),
            "price_basis": CN_PRICE_BASIS_VERSION, "index_basis": CN_INDEX_BASIS_VERSION,
        }

    def _state(self) -> dict:
        value = dict(self._manifest)
        value.update(manifest_path=str(self.manifest_path),
                     source_snapshot=str(self.staging_dir / "source.db"),
                     candidate_db=str(self.staging_dir / "candidate.db"))
        return value

    def _validate_preflight(self):
        report = inspect_cn_initialization(self.source, self.target, self.snapshot, self.cutoff_date)
        if self.target.exists() or self.target.is_symlink():
            raise FileExistsError("CN目标库已存在，禁止覆盖")
        core = ("daily", "adj_factor", "trade_cal", "stock_basic", "index_daily", "constituents", "history_coverage")
        if (not isinstance(self.core_preflight, dict) or not self.core_preflight.get("passed")
                or any(not isinstance(self.core_preflight.get(key), dict)
                       or not self.core_preflight[key].get("passed") for key in core)):
            raise InitializationPreflightError("CN核心接口与历史覆盖预检未通过")
        if report["available_bytes"] < report["required_bytes"]:
            raise InitializationPreflightError("CN初始化可用空间不足")
        if not report["writable"]:
            raise InitializationPreflightError("CN目标目录不可写")
        if not report["same_volume"]:
            raise InitializationPreflightError("CN源快照、staging与目标必须位于同卷")
        return report

    def _recover_published_target(self, candidate: Path) -> bool:
        """发布已落盘而清单尚未更新时，只确认同任务、同指纹的完整目标。"""
        if not self.target.is_file():
            return False
        try:
            with closing(sqlite3.connect(self.target)) as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    return False
                rows = conn.execute(
                    "SELECT item_key,status,fingerprint FROM cn_initialization_state "
                    "WHERE task_id=? AND item_type='stock'", (self.task_id,),
                ).fetchall()
        except sqlite3.Error:
            return False
        completed = {row[0] for row in rows if row[1] == "complete" and row[2] == self._manifest["fingerprint"]}
        if completed != set(self._manifest["stocks"]):
            return False
        if candidate.is_file():
            if _sha256_file(candidate) != _sha256_file(self.target):
                return False
            candidate.unlink()
        self._manifest["stocks"] = {code: "complete" for code in self._manifest["stocks"]}
        self._manifest["status"] = "published"
        self._manifest["published_at"] = self._manifest.get("published_at") or datetime.now(timezone.utc).isoformat()
        self._manifest["target_sha256"] = _sha256_file(self.target)
        _atomic_manifest(self.manifest_path, self._manifest)
        return True

    def _verify_completed(self):
        candidate = Path(self._manifest["candidate_db"])
        if not Path(self._manifest["source_snapshot"]).is_file():
            raise ResumeMismatchError("CN初始化已固定文件缺失")
        if self.target.exists():
            if self._recover_published_target(candidate):
                return
            raise FileExistsError("CN目标库已存在且不属于当前完整任务，禁止覆盖")
        if not candidate.is_file():
            raise ResumeMismatchError("CN初始化已固定文件缺失")
        with closing(sqlite3.connect(candidate)) as conn:
            for table, state in self._manifest.get("tables", {}).items():
                if state.get("status") == "complete":
                    count, digest = _rows_digest(conn, table)
                    if (count, digest) != (state["rows"], state["sha256"]):
                        raise ResumeMismatchError(f"CN初始化已完成项被修改：{table}")
            persisted = {row[0]: row[1] for row in conn.execute(
                "SELECT item_key,status FROM cn_initialization_state WHERE task_id=? AND item_type='stock'",
                (self.task_id,),
            )}
        for code, status in persisted.items():
            if status == "complete":
                self._manifest["stocks"][code] = "complete"
        _atomic_manifest(self.manifest_path, self._manifest)

    def _create_staging(self, report):
        self.staging_dir.mkdir(mode=0o700, exist_ok=False)
        source_snapshot = self.staging_dir / "source.db"
        candidate = self.staging_dir / "candidate.db"
        backup_database(source_snapshot, source_path=self.source)
        source_sha = _sha256_file(source_snapshot)
        with market_context("CN", candidate):
            init_database()
            with get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS cn_initialization_state(
                        task_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                        item_type TEXT NOT NULL, item_key TEXT NOT NULL,
                        status TEXT NOT NULL, checksum TEXT,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(task_id,item_type,item_key)
                    )
                """)
        identity = self._identity(source_sha)
        fingerprint = _json_hash(identity)
        fixed_source = _logical_source_report(source_snapshot)
        fixed_source["sha256"] = source_sha
        codes = sorted(set(report["source"]["codes"]) | set(self.snapshot["codes"]))
        self._manifest = {
            **identity, "task_id": self.task_id, "fingerprint": fingerprint,
            "status": "staging", "created_at": datetime.now(timezone.utc).isoformat(),
            "manifest_path": str(self.manifest_path), "source_snapshot": str(source_snapshot),
            "candidate_db": str(candidate), "core_preflight": self.core_preflight,
            "constituent_snapshot": self.snapshot, "source_summary": fixed_source,
            "tables": {}, "historical_only": {}, "stocks": {code: "pending" for code in [*codes, "000300.SH"]},
        }
        _atomic_manifest(self.manifest_path, self._manifest)
        self._migrate(codes)

    def _migrate(self, codes):
        source_snapshot = Path(self._manifest["source_snapshot"])
        candidate = Path(self._manifest["candidate_db"])
        with closing(sqlite3.connect(source_snapshot)) as source, market_context("CN", candidate), get_connection() as target:
            source.row_factory = sqlite3.Row
            target.execute("BEGIN IMMEDIATE")
            for table in _PERSONAL_TABLES:
                state = _copy_table(source, target, table)
                self._manifest["tables"][table] = state
                target.execute(
                    "INSERT OR REPLACE INTO cn_initialization_state(task_id,fingerprint,item_type,item_key,status,checksum) "
                    "VALUES (?,?,?,?,?,?)",
                    (self.task_id, self._manifest["fingerprint"], "table", table, "complete", state["sha256"]),
                )
            historical = _table_names(source)
            for table in _HISTORICAL_TABLES:
                self._manifest["historical_only"][table] = (
                    source.execute(f"SELECT COUNT(*) FROM {_quoted(table)}").fetchone()[0] if table in historical else 0)
            basic_rows = []
            if "stock_basic" in historical:
                basic_rows = [dict(row) for row in source.execute(
                    "SELECT * FROM stock_basic WHERE ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ'")]
            by_code = {row["ts_code"]: row for row in basic_rows}
            assets = []
            constituents = set(self.snapshot["codes"])
            for code in codes:
                row = by_code.get(code, {})
                assets.append({**row, "ts_code": code, "asset_type": "stock", "currency": "CNY",
                               "sources": ["legacy"] + ([self.snapshot["source"]] if code in constituents else []),
                               "is_seed": True, "is_hs300": code in constituents,
                               "constituent_date": self.snapshot["trade_date"] if code in constituents else None,
                               "snapshot_sha256": self.snapshot["sha256"] if code in constituents else None})
            assets.append({"ts_code": "000300.SH", "name": "沪深300", "market": "A股指数",
                           "asset_type": "index", "currency": "CNY", "sources": [self.snapshot["source"]],
                           "is_seed": True, "is_hs300": False})
            register_cn_assets(target, assets)
            rotate_data_epoch(target)
        _atomic_manifest(self.manifest_path, self._manifest)

    def start(self) -> dict:
        if self._manifest is not None:
            self._verify_completed()
            return self._state()
        report = self._validate_preflight()
        self._create_staging(report)
        return self._state()

    def _progress(self, code):
        fingerprint, task_id = self._manifest["fingerprint"], self.task_id
        def record(conn, candidate):
            checksum = _json_hash({"code": code, "anchor": candidate["anchor_date"],
                                   "basis": candidate["basis_version"]})
            conn.execute(
                "INSERT OR REPLACE INTO cn_initialization_state(task_id,fingerprint,item_type,item_key,status,checksum) "
                "VALUES (?,?,?,?,?,?)", (task_id, fingerprint, "stock", code, "complete", checksum),
            )
        return record

    def _validate_candidate(self):
        candidate = Path(self._manifest["candidate_db"])
        with closing(sqlite3.connect(candidate)) as conn:
            conn.row_factory = sqlite3.Row
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("CN候选库完整性校验失败")
            pending = [code for code, state in self._manifest["stocks"].items() if state != "complete"]
            if pending:
                raise RuntimeError("CN候选库仍有未完成标的")
            count = conn.execute("SELECT COUNT(*) FROM cn_daily_publication").fetchone()[0]
            if count != len(self._manifest["stocks"]):
                raise RuntimeError("CN候选库发布记录不完整")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.commit()
        for suffix in ("-wal", "-shm"):
            if Path(str(candidate) + suffix).exists():
                raise RuntimeError("CN候选库WAL未收束")

    def _publish(self):
        candidate = Path(self._manifest["candidate_db"])
        if self.target.exists() or self.target.is_symlink():
            raise FileExistsError("CN目标库已存在，禁止覆盖")
        try:
            os.link(candidate, self.target)
        except FileExistsError:
            raise FileExistsError("CN目标库已存在，禁止覆盖") from None
        except OSError as exc:
            raise RuntimeError("CN候选库必须同卷拒绝覆盖发布") from exc
        candidate.unlink()
        self._manifest["status"] = "published"
        self._manifest["published_at"] = datetime.now(timezone.utc).isoformat()
        self._manifest["target_sha256"] = _sha256_file(self.target)
        _atomic_manifest(self.manifest_path, self._manifest)

    def run(self) -> dict:
        self.start()
        if self.stock_initializer is None:
            raise InitializationPreflightError("CN初始化缺少已核验的逐股行情初始化器")
        candidate = Path(self._manifest["candidate_db"])
        for code in sorted(self._manifest["stocks"]):
            if self._manifest["stocks"][code] == "complete":
                continue
            with market_context("CN", candidate):
                self.stock_initializer(code, self.cutoff_date, self._progress(code))
                with get_connection() as conn:
                    done = conn.execute(
                        "SELECT 1 FROM cn_initialization_state WHERE task_id=? AND item_type='stock' "
                        "AND item_key=? AND status='complete'", (self.task_id, code),
                    ).fetchone()
                if not done:
                    raise RuntimeError("逐股初始化未与行情发布事务共同记录进度")
            self._manifest["stocks"][code] = "complete"
            _atomic_manifest(self.manifest_path, self._manifest)
        self._validate_candidate()
        self._publish()
        return self._state()
