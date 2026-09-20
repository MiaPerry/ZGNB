"""
A 股一键批量同步服务

- 从 stock_basic 与 daily_kline 的并集动态收集当前库内 A 股名单
- 按北京时间计算查询上界（18:00 后允许当天，周末回退）
- 单工作线程后台执行，重复提交返回同一任务
- 写入前通过 SQLite backup API 备份，限流/网络错误分级重试，鉴权错误立即终止
"""

import logging
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.database import get_connection, get_db_path

logger = logging.getLogger(__name__)

# A 股代码后缀（不含美股 .US / 港股 .HK 等）
_A_SHARE_SUFFIXES = (".SH", ".SZ", ".BJ")

# 收盘后查询边界：18:00（北京时间）之后允许查询当天完整日线
_CLOSE_HOUR = 18

# 错误分类关键字
_RATE_LIMIT_KEYWORDS = ("最多访问", "限频", "频繁", "too many", "rate limit", "exceed")
_AUTH_KEYWORDS = ("token", "权限", "未授权", "unauthorized", "forbidden", "401", "403")
_NETWORK_KEYWORDS = ("timeout", "timed out", "connection", "网络", "unreachable", "reset")


def _is_a_share(ts_code: str) -> bool:
    return ts_code.upper().endswith(_A_SHARE_SUFFIXES)


def collect_a_share_codes() -> list[str]:
    """当前库内全部 A 股代码：stock_basic 与 daily_kline 并集，去重排序"""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT ts_code FROM stock_basic
            UNION
            SELECT ts_code FROM daily_kline
            """
        ).fetchall()
    codes = {row[0] for row in rows if row[0] and _is_a_share(row[0])}
    return sorted(codes)


def compute_query_end_date(now: datetime) -> str:
    """
    计算 Tushare 查询上界（北京时间）

    - 18:00 后允许查询当天完整日线
    - 18:00 前查询前一天
    - 结果落在周末则回退到周五；节假日由数据源返回结果体现
    """
    day = now.date() if now.hour >= _CLOSE_HOUR else (now - timedelta(days=1)).date()
    while day.weekday() >= 5:  # 5=周六 6=周日
        day -= timedelta(days=1)
    return day.strftime("%Y%m%d")


def _beijing_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def classify_error(exc: Exception) -> str:
    """将异常分类为 rate_limit / auth / network / other"""
    msg = str(exc).lower()
    if any(k in msg for k in _RATE_LIMIT_KEYWORDS):
        return "rate_limit"
    if any(k in msg for k in _AUTH_KEYWORDS):
        return "auth"
    if isinstance(exc, (ConnectionError, TimeoutError)) or any(k in msg for k in _NETWORK_KEYWORDS):
        return "network"
    return "other"


class SyncConfigError(Exception):
    """同步配置缺失（如 TUSHARE_TOKEN 未配置）"""


class _AbortTask(Exception):
    """任务级终止（如鉴权失败），不再处理后续股票"""


# ==================== 任务快照 ====================


@dataclass
class SyncTaskSnapshot:
    """同步任务状态快照（前端轮询展示用，全部字段可 JSON 序列化）"""

    task_id: str | None = None
    status: str = "idle"  # idle / running / completed / partial_failure / failed
    phase: str = "idle"  # idle / backup / sync / indicators / done
    current_code: str | None = None
    total: int = 0
    processed: int = 0
    success: int = 0
    no_change: int = 0
    failed: int = 0
    new_rows: int = 0
    data_date: str | None = None  # 同步后库内实际最新行情日期
    message: str = ""
    failures: list[dict] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def backup_database(dest_path: Path) -> Path:
    """通过 SQLite backup API 备份当前数据库（WAL 安全），并做完整性校验"""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{get_db_path()}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
            result = dest.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"备份完整性校验失败: {result}")
        finally:
            dest.close()
    finally:
        src.close()
    return dest_path


# ==================== 批量同步服务 ====================


class AShareSyncService:
    """A 股一键批量同步：单工作线程后台执行，重复提交返回同一任务"""

    def __init__(
        self,
        syncer=None,
        sleep=time.sleep,
        now_provider=_beijing_now,
        backup_dir: Path | None = None,
        rate_limit_wait: float = 65.0,
        network_backoff: float = 5.0,
        max_attempts: int = 3,
    ):
        self._syncer = syncer
        self._sleep = sleep
        self._now = now_provider
        self._backup_dir = Path(backup_dir) if backup_dir else get_db_path().parent / "backups"
        self._rate_limit_wait = rate_limit_wait
        self._network_backoff = network_backoff
        self._max_attempts = max_attempts

        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="a-share-sync")
        self._snapshot = SyncTaskSnapshot()
        self._future = None

    # ---------- 公开接口 ----------

    def submit(self) -> SyncTaskSnapshot:
        """提交同步任务；已有运行中的任务时直接返回当前任务快照"""
        self._ensure_config()
        with self._lock:
            if self._snapshot.status == "running":
                return self._copy_snapshot()
            self._snapshot = SyncTaskSnapshot(
                task_id=uuid.uuid4().hex[:12],
                status="running",
                phase="backup",
                message="任务已启动",
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._future = self._executor.submit(self._run)
            return self._copy_snapshot()

    def get_status(self) -> SyncTaskSnapshot:
        """当前或最近一次任务快照；从未运行过返回 idle"""
        with self._lock:
            return self._copy_snapshot()

    def wait_for_completion(self, timeout: float = 30) -> bool:
        """等待当前任务结束（测试/验收用），超时返回 False"""
        future = self._future
        if future is None:
            return True
        try:
            future.result(timeout=timeout)
            return True
        except TimeoutError:
            return False
        except Exception:
            return True  # 异常已计入快照

    # ---------- 内部实现 ----------

    def _copy_snapshot(self) -> SyncTaskSnapshot:
        snap = self._snapshot
        return SyncTaskSnapshot(**{**asdict(snap), "failures": [dict(f) for f in snap.failures]})

    def _update(self, **fields):
        with self._lock:
            for key, value in fields.items():
                setattr(self._snapshot, key, value)

    def _ensure_config(self):
        import os

        # 仅在需要自建真实同步器时检查配置；测试注入同步器时不检查
        if self._syncer is None and not os.environ.get("TUSHARE_TOKEN"):
            raise SyncConfigError("缺少 TUSHARE_TOKEN 配置，请先在 .env 中配置后再同步")

    def _get_syncer(self):
        if self._syncer is None:
            from modules.data_sync import DataSyncer

            self._syncer = DataSyncer()
        return self._syncer

    def _run(self):
        try:
            self._run_inner()
        except Exception as e:  # 兜底：任何未预期异常都必须落到快照
            logger.exception("A 股批量同步任务异常终止")
            self._update(
                status="failed",
                phase="done",
                message=f"任务异常终止: {e}",
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )

    def _run_inner(self):
        codes = collect_a_share_codes()
        self._update(total=len(codes))
        if not codes:
            self._update(
                status="completed",
                phase="done",
                message="当前库中没有可同步的 A 股",
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            return

        # 写入前备份；失败则停止，不碰数据
        try:
            backup_database(self._backup_dir / "a-share-pre-sync.db")
        except Exception as e:
            self._update(
                status="failed",
                phase="done",
                message=f"备份失败，已停止同步: {e}",
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            return

        syncer = self._get_syncer()
        end_date = compute_query_end_date(self._now())
        aborted = None

        for code in codes:
            self._update(current_code=code, phase="sync")
            try:
                outcome = self._sync_one(syncer, code, end_date)
            except _AbortTask as e:
                aborted = str(e)
                break

            snap = self._snapshot
            updates = {"processed": snap.processed + 1}
            if outcome["kind"] == "success":
                updates["success"] = snap.success + 1
                updates["new_rows"] = snap.new_rows + outcome["added"]
            elif outcome["kind"] == "no_change":
                updates["no_change"] = snap.no_change + 1
            else:
                updates["failed"] = snap.failed + 1
                updates["failures"] = snap.failures + [{"ts_code": code, "error": outcome["error"]}]
            self._update(**updates)

        snap = self._snapshot
        data_date = self._latest_data_date()
        finished = datetime.now().isoformat(timespec="seconds")
        if aborted is not None:
            self._update(
                status="failed",
                phase="done",
                data_date=data_date,
                message=f"任务终止: {aborted}",
                finished_at=finished,
            )
        elif snap.failed > 0:
            status = "partial_failure" if snap.processed > snap.failed else "failed"
            self._update(
                status=status,
                phase="done",
                data_date=data_date,
                message=f"完成，{snap.failed} 只股票同步失败",
                finished_at=finished,
            )
        else:
            self._update(
                status="completed",
                phase="done",
                data_date=data_date,
                message="同步完成",
                finished_at=finished,
            )

    def _sync_one(self, syncer, code: str, end_date: str) -> dict:
        """同步单只股票，返回 {kind: success/no_change/failed, added, error}"""
        start_date = self._incremental_start(code)
        if start_date > end_date:
            # 库内数据已覆盖查询上界，无需请求接口
            self._recompute_if_indicator_mismatch(syncer, code)
            return {"kind": "no_change", "added": 0}

        added, error = self._sync_with_retry(syncer, code, start_date, end_date)
        if error is not None:
            return {"kind": "failed", "added": 0, "error": error}

        if added > 0:
            self._recompute_indicators(syncer, code)
            return {"kind": "success", "added": added}

        self._recompute_if_indicator_mismatch(syncer, code)
        return {"kind": "no_change", "added": 0}

    def _incremental_start(self, code: str) -> str:
        """增量起点 = 库内真实 K 线最大日期 +1；无行情时首拉最近 730 个自然日"""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT MAX(trade_date) FROM daily_kline WHERE ts_code = ?", (code,)
            ).fetchone()
        last_date = row[0] if row else None
        if last_date:
            return (datetime.strptime(last_date, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
        return (self._now() - timedelta(days=730)).strftime("%Y%m%d")

    def _sync_with_retry(self, syncer, code: str, start_date: str, end_date: str):
        """带分级重试的日线同步；返回 (added, error)。鉴权错误抛 _AbortTask"""
        for attempt in range(1, self._max_attempts + 1):
            try:
                added = syncer.sync_daily_kline(
                    code, start_date=start_date, end_date=end_date, raise_on_error=True
                )
                return added, None
            except Exception as e:
                kind = classify_error(e)
                logger.warning("日线同步失败 %s（第 %d 次，分类 %s）: %s", code, attempt, kind, e)
                if kind == "auth":
                    raise _AbortTask(f"鉴权或配置错误（{code}）: {e}") from e
                if attempt >= self._max_attempts:
                    return None, str(e)
                self._sleep(self._rate_limit_wait if kind == "rate_limit" else self._network_backoff * attempt)
        return None, "超过最大重试次数"

    def _recompute_indicators(self, syncer, code: str):
        """按该股票全部已有历史条数重算指标，保证递推指标预热；失败计入该股票失败"""
        self._update(phase="indicators")
        days = self._kline_count(code)
        try:
            syncer.sync_indicator_cache(code, days=days)
        except Exception as e:
            raise RuntimeError(f"指标重算失败: {e}") from e

    def _recompute_if_indicator_mismatch(self, syncer, code: str):
        """指标缓存条数与 K 线不一致时补算"""
        with get_connection() as conn:
            indicator_count = conn.execute(
                "SELECT COUNT(*) FROM indicator_cache WHERE ts_code = ?", (code,)
            ).fetchone()[0]
        if indicator_count != self._kline_count(code):
            self._recompute_indicators(syncer, code)

    @staticmethod
    def _kline_count(code: str) -> int:
        with get_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM daily_kline WHERE ts_code = ?", (code,)
            ).fetchone()[0]

    @staticmethod
    def _latest_data_date() -> str | None:
        with get_connection() as conn:
            row = conn.execute("SELECT MAX(trade_date) FROM daily_kline").fetchone()
        return row[0] if row else None


# ==================== 服务单例 ====================

_service: AShareSyncService | None = None
_service_lock = threading.Lock()


def get_a_share_sync_service() -> AShareSyncService:
    """获取全局同步服务单例（任务状态驻留后端内存，单 API 进程部署）"""
    global _service
    with _service_lock:
        if _service is None:
            _service = AShareSyncService()
        return _service
