"""网页行情写任务的跨线程/进程互斥；进程退出时系统自动释放锁。"""

import os
from contextlib import AbstractContextManager

from .database import get_db_path


class SyncBusyError(RuntimeError):
    """已有行情写任务运行，调用方应返回 409。"""


class MarketSyncLease(AbstractContextManager):
    def __init__(self, handle):
        self.handle = handle

    @staticmethod
    def _path():
        return get_db_path().with_suffix(".market-sync.lock")

    @staticmethod
    def _lock(handle):
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @classmethod
    def acquire(cls, owner):
        handle = os.fdopen(os.open(cls._path(), os.O_CREAT | os.O_RDWR, 0o600), "r+b", buffering=0)
        try:
            cls._lock(handle)
        except OSError:
            handle.close()
            raise SyncBusyError("已有行情同步或股票添加任务运行，请完成后重试") from None
        try:
            handle.write(b"\0" + owner.encode("ascii"))
            handle.truncate()
            return cls(handle)
        except BaseException:
            handle.close()
            raise

    @classmethod
    def is_running(cls, owner):
        try:
            handle = cls._path().open("r+b", buffering=0)
        except FileNotFoundError:
            return False
        try:
            try:
                cls._lock(handle)
            except OSError:
                # Windows 仅锁住首字节，读取其后的任务 ID 不触及锁区间。
                handle.seek(1)
                return handle.read(128).decode("ascii") == owner
            return False
        finally:
            handle.close()

    def __exit__(self, *args):
        self.handle.close()
