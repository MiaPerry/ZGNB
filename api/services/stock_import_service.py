"""普通美股单只/批量收录，任务状态与实际行情分开记录。"""

import copy
import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from modules.database import get_connection, get_db_path
from modules.data_freshness import data_update_batch
from modules.indicators.cache_builder import missing_dates, write_indicator_rows
from modules.market_sync_lock import MarketSyncLease, SyncBusyError
from modules.yahoo_sync import import_us_stock
from api.services.sync_service import _beijing_now, backup_database, classify_error, compute_market_end_date

logger = logging.getLogger(__name__)
MAX_IMPORT_CODES = 50
_TERMINAL_ITEMS = {"completed", "skipped", "failed", "interrupted"}


def normalize_codes(text: str) -> list[str]:
    """只接受股票代码；拒绝市场后缀、指数及路径等非普通美股输入。"""
    if not isinstance(text, str) or len(text) > 5000:
        raise ValueError("请输入不超过 5000 字符的美股代码")
    codes = []
    for token in re.split(r"[\s,，]+", text.strip().upper()):
        ticker = token.removesuffix(".US")
        if not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:[.-][A-Z0-9])?", ticker) or ticker in {"SPX", "DJI", "IXIC"}:
            raise ValueError(f"无效或不支持的美股代码：{token[:40] or '（空）'}")
        code = ticker.replace(".", "-") + ".US"
        if code not in codes:
            codes.append(code)
    if len(codes) > MAX_IMPORT_CODES:
        raise ValueError(f"每次最多添加 {MAX_IMPORT_CODES} 只股票")
    return codes


def _summarize(job):
    job["total"] = len(job["items"])
    job["success"] = sum(item["status"] == "completed" for item in job["items"])
    job["skipped"] = sum(item["status"] == "skipped" for item in job["items"])
    job["failed"] = sum(item["status"] in ("failed", "interrupted") for item in job["items"])
    job["processed"] = sum(item["status"] in _TERMINAL_ITEMS for item in job["items"])
    return job


class StockImportService:
    """持久任务 + 单线程执行；系统锁与其他网页行情写任务共用。"""

    def __init__(
        self,
        *,
        fetch=import_us_stock,
        compute=write_indicator_rows,
        sleep=time.sleep,
        now=_beijing_now,
        backup_dir=None,
    ):
        self.fetch, self.compute, self.sleep, self.now = fetch, compute, sleep, now
        self.backup_dir = Path(backup_dir) if backup_dir else None
        self._mutex = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stock-import")
        self._future = None

    def get_status(self, task_id=None):
        with get_connection() as conn:
            row = conn.execute(
                "SELECT message FROM sync_log WHERE data_type='stock_import' "
                + ("AND ts_code=? " if task_id else "")
                + "ORDER BY id DESC LIMIT 1",
                (task_id,) if task_id else (),
            ).fetchone()
        if not row:
            if task_id:
                raise KeyError("股票添加任务不存在")
            return {
                "task_id": None,
                "status": "idle",
                "phase": "idle",
                "items": [],
                "total": 0,
                "processed": 0,
                "success": 0,
                "skipped": 0,
                "failed": 0,
                "message": "",
            }
        job = json.loads(row[0])
        if job["status"] == "running" and not MarketSyncLease.is_running(job["task_id"]):
            # 读取不写库；系统锁已释放而持久状态未结束，说明进程中断。
            job.update(status="interrupted", phase="done", message="任务已中断，可以重试未完成项")
            for item in job["items"]:
                if item["status"] not in _TERMINAL_ITEMS:
                    item.update(status="interrupted", message="执行中断，请重试")
        return _summarize(job)

    def submit(self, text):
        return self._submit(normalize_codes(text))

    def retry(self, task_id):
        previous = self.get_status(task_id)
        if previous["status"] == "running":
            raise SyncBusyError("该任务仍在运行")
        items = [item for item in previous["items"] if item["status"] in ("failed", "interrupted")]
        if not items:
            raise ValueError("该任务没有可重试的失败项")
        return self._submit([item["ts_code"] for item in items], previous=previous)

    def _submit(self, codes, previous=None):
        with self._mutex:
            task_id = uuid.uuid4().hex
            parent = previous["task_id"] if previous else None
            try:
                lease = MarketSyncLease.acquire(task_id)
            except SyncBusyError:
                current = self.get_status()
                if (
                    current["status"] == "running"
                    and current.get("codes") == codes
                    and current.get("parent_id") == parent
                ):
                    return current
                raise
            try:
                now = self.now()
                try:
                    start = now.replace(year=now.year - 5).strftime("%Y%m%d")
                except ValueError:
                    start = now.replace(year=now.year - 5, day=28).strftime("%Y%m%d")
                with get_connection() as conn:
                    earliest = conn.execute(
                        "SELECT MIN(k.trade_date) FROM daily_kline k JOIN stock_basic s ON s.ts_code=k.ts_code "
                        "WHERE s.market='美股' AND s.ts_code LIKE '%.US'"
                    ).fetchone()[0]
                if earliest:
                    start = min(start, earliest)
                items = [
                    {
                        "ts_code": code,
                        "status": "pending",
                        "owned": False,
                        "message": "",
                        "rows": 0,
                        "indicator_rows": 0,
                        "first_date": None,
                        "last_date": None,
                        "name": "",
                    }
                    for code in codes
                ]
                if previous:
                    items = [copy.deepcopy(item) for item in previous["items"] if item["ts_code"] in codes]
                    for item in items:
                        item.update(status="pending", message="")
                job = {
                    "task_id": task_id,
                    "parent_id": parent,
                    "codes": codes,
                    "status": "running",
                    "phase": "backup",
                    "message": "任务已启动",
                    "items": items,
                    "start_date": previous["start_date"] if previous else start,
                    "end_date": previous["end_date"] if previous else compute_market_end_date(now, "US"),
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "finished_at": None,
                }
                self._save(job, create=True)
                initial = copy.deepcopy(job)
                self._future = self._executor.submit(self._run, job, lease)
                return initial
            except BaseException:
                lease.__exit__()
                raise

    @staticmethod
    def _save(job, create=False):
        _summarize(job)
        job["revision"] = job.get("revision", 0) + 1
        with get_connection() as conn:
            if create:
                job["sequence"] = conn.execute(
                    "INSERT INTO sync_log(data_type,ts_code,status,message) VALUES ('stock_import',?,?,'{}')",
                    (job["task_id"], job["status"]),
                ).lastrowid
            payload = json.dumps(job, ensure_ascii=False, allow_nan=False)
            conn.execute(
                "UPDATE sync_log SET status=?,message=? WHERE data_type='stock_import' AND ts_code=?",
                (job["status"], payload, job["task_id"]),
            )

    def _run(self, job, lease):
        with lease:
            try:
                dest = self.backup_dir or get_db_path().parent / "backups"
                backup_database(dest / f"pre-stock-import-{job['task_id']}.db")
                with data_update_batch():
                    for index, item in enumerate(job["items"]):
                        try:
                            self._process(job, item)
                        except Exception as exc:
                            logger.warning("股票收录失败 %s：%s", item["ts_code"], type(exc).__name__)
                            # 不向任务状态泄漏代理、响应正文或其他底层异常文本。
                            if item["status"] == "indicators":
                                message = "指标计算失败，可重试补齐"
                            elif isinstance(exc, ValueError):
                                # import_us_stock 的 ValueError 文案均为受控的安全校验原因。
                                message = str(exc)[:160]
                            elif isinstance(exc, (ConnectionError, TimeoutError)):
                                message = "行情请求失败，请检查网络或数据源后重试"
                            else:
                                message = "行情下载或校验失败，请核对代码及数据源后重试"
                            item.update(status="failed", message=message)
                        self._save(job)
                        if index + 1 < len(job["items"]) and item["status"] != "skipped":
                            self.sleep(1.5)
                _summarize(job)
                status = (
                    ("partial_failure" if job["success"] + job["skipped"] else "failed")
                    if job["failed"]
                    else "completed"
                )
                job.update(
                    status=status,
                    phase="done",
                    message="处理结束，请查看逐只结果",
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
                self._save(job)
            except Exception as exc:
                logger.warning("股票收录任务终止：%s", type(exc).__name__)
                for item in job["items"]:
                    if item["status"] not in _TERMINAL_ITEMS:
                        item.update(status="failed", message="任务中断，可重试；如反复失败请检查备份与数据库")
                job.update(
                    status="failed",
                    phase="done",
                    message="任务未完成；已保存的数据保留，可重试失败项",
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
                self._save(job)

    @staticmethod
    def _stock_state(code):
        with get_connection() as conn:
            basic = conn.execute("SELECT name FROM stock_basic WHERE ts_code=?", (code,)).fetchone()
            bars = conn.execute(
                "SELECT COUNT(*),MIN(trade_date),MAX(trade_date) FROM daily_kline WHERE ts_code=?", (code,)
            ).fetchone()
            indicators = conn.execute("SELECT COUNT(*) FROM indicator_cache WHERE ts_code=?", (code,)).fetchone()[0]
            missing = missing_dates(conn, code)
        return basic, bars, indicators, missing

    def _process(self, job, item):
        code = item["ts_code"]
        basic, bars, _, _ = self._stock_state(code)
        if (basic or bars[0]) and not item.get("owned"):
            item.update(status="skipped", message="已收录，未重复添加或覆盖；请使用日常更新")
            return
        # 标记收录归属后才写行情，崩溃重试可识别本任务的已提交日线。
        item.update(owned=True, status="downloading", message="下载并校验历史日线")
        job["phase"] = "download"
        self._save(job)
        if not bars[0]:
            for attempt in range(3):
                try:
                    self.fetch(code, job["start_date"], job["end_date"])
                    break
                except (ConnectionError, TimeoutError) as exc:
                    if attempt == 2:
                        raise
                    item["message"] = f"行情请求失败，等待第 {attempt + 2} 次尝试"
                    self._save(job)
                    self.sleep(65 if classify_error(exc) == "rate_limit" else 5 * (attempt + 1))
        basic, bars, _, _ = self._stock_state(code)
        if not basic or not bars[0]:
            raise ValueError("没有有效行情，未完成收录")
        item.update(
            name=basic[0],
            rows=bars[0],
            first_date=bars[1],
            last_date=bars[2],
            status="indicators",
            message="补算全部缺失日期指标",
        )
        job["phase"] = "indicators"
        self._save(job)
        self.compute(code)
        _, bars, indicators, missing = self._stock_state(code)
        if missing or indicators != bars[0]:
            raise RuntimeError("行情与指标日期未完整对应")
        item.update(status="completed", indicator_rows=indicators, message="行情及指标已就绪")

    def wait_for_completion(self, timeout=30):
        if self._future:
            self._future.result(timeout=timeout)
        return True

    def shutdown(self):
        self._executor.shutdown(wait=True)


_service = None
_service_lock = threading.Lock()


def get_stock_import_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = StockImportService()
        return _service
