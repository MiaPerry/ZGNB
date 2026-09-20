"""批量同步任务响应模型"""

from pydantic import BaseModel


class SyncFailureItem(BaseModel):
    ts_code: str
    error: str


class BatchSyncSnapshot(BaseModel):
    """A 股批量同步任务快照（前端轮询展示）"""

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
    data_date: str | None = None
    message: str = ""
    failures: list[SyncFailureItem] = []
    started_at: str | None = None
    finished_at: str | None = None
