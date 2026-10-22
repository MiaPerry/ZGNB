"""
数据同步模块
从 Tushare API 获取数据并存储到 SQLite
支持增量更新和全量更新
"""

import os
import time
import logging
import threading
import collections
import multiprocessing
import concurrent.futures
from datetime import datetime, timedelta
from typing import Any, Optional

try:
    import tushare as ts
except ImportError:
    print("请先安装依赖: pip install tushare")

# dotenv 加载已移至 modules/__init__.py（包级别一次性加载，override=True）

from .database import get_connection, get_db_path

logger = logging.getLogger(__name__)

# 并发同步线程数（4 个 sync_* 方法共用）
_MAX_SYNC_WORKERS = 5

# 涨跌停阈值（主板 10%，此处用 9.9% 容差）
# 注意：创业板(300xxx)/科创板(688xxx) 实际为 20%，ST 为 5%，
# 新股前 5 日无限制。当前简化处理，v2.11.0 计划按 market 字段动态调整。
_LIMIT_THRESHOLD = 9.9

# 中转 API 配置（从环境变量读取）
TUSHARE_API_URL = os.environ.get("TUSHARE_API_URL", "")
VERIFY_TOKEN_URL = os.environ.get("TUSHARE_VERIFY_TOKEN_URL", "")


# ==================== 模块级限流器（v2.10.0 P1-4） ====================
# 多进程安全：同机多进程共享同一把 multiprocessing.Lock
# 限流仅同机多进程有效，跨机器需 Redis 协调（详见 plan P1-4 风险）
class _RateLimiter:
    """Tushare 限流器（多进程安全 + 滑动窗口 token bucket）

    设计：
    - 60s 滑动窗口内的请求计数（in-memory deque）
    - multiprocessing.Lock 序列化 critical section
    - TUSHARE_RPM env var 控制 max requests/min（默认 180，留 20 缓冲应对 200 上限）

    用法：
        _GLOBAL_LIMITER.wait()  # 阻塞直到安全可调
    """

    def __init__(self, max_per_min: int = 180):
        self._max = max_per_min
        self._window: collections.deque = collections.deque()
        # 关键：multiprocessing.Lock 不是进程间共享的默认锁
        # 在父进程创建，子进程 fork 后会继承一份
        self._lock = multiprocessing.Lock()

    def wait(self) -> None:
        """阻塞直到 60s 窗口内有空位"""
        with self._lock:
            now = time.monotonic()
            # 弹出 60s 外的旧时间戳
            while self._window and (now - self._window[0]) > 60:
                self._window.popleft()
            if len(self._window) >= self._max:
                # 等待最老一项出窗口
                sleep_for = 60 - (now - self._window[0]) + 0.05  # +0.05s 缓冲
                logger.debug(f"限流：等 {sleep_for:.2f}s（窗口已满 {self._max} req）")
                time.sleep(sleep_for)
                # 重新弹出（防止极端情况）
                now = time.monotonic()
                while self._window and (now - self._window[0]) > 60:
                    self._window.popleft()
            self._window.append(time.monotonic())

    @property
    def current_count(self) -> int:
        """当前窗口内请求数（只读，调试用）"""
        with self._lock:
            now = time.monotonic()
            while self._window and (now - self._window[0]) > 60:
                self._window.popleft()
            return len(self._window)


# 模块级单例（v2.10.0 P1-4 替代原 instance-level _rate_limit_lock）
_GLOBAL_LIMITER = _RateLimiter(max_per_min=int(os.environ.get("TUSHARE_RPM", "180")))


def _rate_limit_global() -> None:
    """模块级公开限流入口（v2.10.0 P1-4 新增，替代 instance-level _rate_limit）"""
    _GLOBAL_LIMITER.wait()


def select_cn_constituent_snapshot(frame, cutoff: str, open_dates: set[str], *,
                                   source: str = "tushare.index_weight:399300.SZ") -> dict:
    """按单个有效交易日固化完整沪深300快照，不把多日成分混为一份名单。"""
    import hashlib
    import json
    import math
    import pandas as pd
    from .database import validate_market_code

    cutoff_day = datetime.strptime(cutoff, "%Y%m%d")
    required = {"index_code", "trade_date", "con_code", "weight"}
    if not isinstance(frame, pd.DataFrame) or not required.issubset(frame.columns):
        raise ValueError("沪深300成分快照结构缺失")
    for date, group in sorted(frame.groupby("trade_date"), key=lambda item: str(item[0]), reverse=True):
        try:
            date = str(date)
            age = (cutoff_day - datetime.strptime(date, "%Y%m%d")).days
            if date not in open_dates or not 0 <= age <= 62 or len(group) != 300:
                continue
            if set(group["index_code"]) != {"399300.SZ"}:
                continue
            pairs = sorted((validate_market_code(str(row.con_code), "CN"), float(row.weight))
                           for row in group.itertuples())
            codes = [code for code, _ in pairs]
            if len(set(codes)) != 300 or "000300.SH" in codes:
                continue
            if any(not math.isfinite(weight) or weight <= 0 for _, weight in pairs):
                continue
            if not 99.5 <= sum(weight for _, weight in pairs) <= 100.5:
                continue
            result = {"source": source, "trade_date": date,
                      "codes": codes, "weights": dict(pairs)}
            serialized = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            result["sha256"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            return result
        except (ValueError, TypeError):
            continue
    raise ValueError("沪深300没有完整且不超过62天的有效成分快照")


CSINDEX_CLOSEWEIGHT_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/"
    "closeweight/000300closeweight.xls"
)


def parse_csindex_closeweight(content: bytes, cutoff: str, open_dates: set[str]) -> dict:
    """解析中证官网月末权重文件；仍严格验证单日300成分、交易日及时效。"""
    import re
    import pandas as pd
    import xlrd

    if not content or len(content) > 2 * 1024 * 1024:
        raise ValueError("中证权重文件为空或超过大小限制")
    try:
        sheet = xlrd.open_workbook(file_contents=content).sheet_by_index(0)
    except (xlrd.XLRDError, IndexError, ValueError):
        raise ValueError("中证权重文件不是有效xls") from None
    if sheet.nrows != 301 or sheet.ncols != 10:
        raise ValueError("中证权重文件不是完整300成分结构")
    headers = [str(value).lower().replace(" ", "") for value in sheet.row_values(0)]
    if any(token not in headers[column] for column, token in
           ((0, "date"), (1, "indexcode"), (4, "constituentcode"), (8, "exchange(eng)"), (9, "weight"))):
        raise ValueError("中证权重文件列结构无效")
    exchanges = {"Shanghai Stock Exchange": "SH", "Shenzhen Stock Exchange": "SZ"}
    rows = []
    for number in range(1, sheet.nrows):
        row = sheet.row_values(number)
        code = str(row[4]).strip()
        index = str(row[1]).strip()
        if not re.fullmatch(r"\d{6}", code) or index != "000300":
            raise ValueError("中证成分代码或指数身份无效")
        suffix = exchanges.get(str(row[8]).strip())
        if not suffix:
            raise ValueError("中证成分交易所无效")
        rows.append({"index_code": "399300.SZ", "trade_date": str(row[0]).strip(),
                     "con_code": f"{code}.{suffix}", "weight": row[9]})
    return select_cn_constituent_snapshot(pd.DataFrame(rows), cutoff, open_dates,
                                          source="csindex.closeweight:000300")


def download_csindex_closeweight() -> bytes:
    """只从固定中证公开地址读取有限大小文件，不写数据库或配置。"""
    from urllib.request import urlopen

    with urlopen(CSINDEX_CLOSEWEIGHT_URL, timeout=30) as response:
        content = response.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("中证权重文件超过大小限制")
    return content


def load_cn_constituent_snapshot(client, cutoff: str) -> dict:
    """只读预取最近三个月；调用错误不伪装为空名单，不无限回查过期成分。"""
    import pandas as pd

    cutoff_day = datetime.strptime(cutoff, "%Y%m%d")
    month = cutoff_day.replace(day=1)
    windows = []
    for _ in range(3):
        next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        windows.append((month.strftime("%Y%m%d"), min(cutoff_day, next_month - timedelta(days=1)).strftime("%Y%m%d")))
        month = (month - timedelta(days=1)).replace(day=1)
    calendar = client.get_trade_cal(start_date=windows[-1][0], end_date=cutoff, raise_on_error=True)
    if (not isinstance(calendar, pd.DataFrame) or not {"cal_date", "is_open"}.issubset(calendar.columns)
            or calendar["cal_date"].duplicated().any()
            or not set(calendar["is_open"].astype(str)).issubset({"0", "1"})):
        raise ValueError("CN交易日历结构无效，不能核验成分快照")
    expected = set(pd.date_range(windows[-1][0], cutoff).strftime("%Y%m%d"))
    if not expected.issubset(set(calendar["cal_date"].astype(str))):
        raise ValueError("CN交易日历覆盖缺失，不能核验成分快照")
    open_dates = set(calendar.loc[calendar["is_open"].astype(str) == "1", "cal_date"].astype(str))
    for start, end in windows:
        frame = client.get_index_weight(start, end)
        if isinstance(frame, pd.DataFrame) and "trade_date" in frame.columns:
            frame = frame.loc[frame["trade_date"].astype(str).between(start, end)]
        try:
            return select_cn_constituent_snapshot(frame, cutoff, open_dates)
        except ValueError:
            continue
    raise ValueError("沪深300最近三个月内无完整及时成分快照，禁止初始化名单")


def collect_legacy_cn_assets(conn) -> dict[str, dict]:
    """从固定源快照收集合并引用；不因基本资料缺失而漏掉行情或个人记录。"""
    from .database import validate_market_code

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {}
    for table in ("stock_basic", "daily_kline", "watchlist", "trade_records", "tracking_pool_self",
                  "tracking_records_self", "monthly_reviews_self"):
        if table not in tables:
            continue
        available = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        columns = [key for key in ("ts_code", "name", "area", "industry", "market", "list_date", "is_hs") if key in available]
        for row in conn.execute(f"SELECT DISTINCT {','.join(columns)} FROM \"{table}\" "
                                "WHERE ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ'"):
            info = dict(row)
            code = info.get("ts_code")
            if not isinstance(code, str) or not code.endswith((".SH", ".SZ")):
                continue
            validate_market_code(code, "CN")
            existing = result.setdefault(code, {})
            for key in ("name", "area", "industry", "market", "list_date", "is_hs"):
                if info.get(key) and not existing.get(key):
                    existing[key] = info[key]
    return result


def build_cn_seed_assets(legacy: dict[str, dict], snapshot: dict, basic_rows) -> list[dict]:
    """初始化种子=旧代码并集成分快照；指数单列，不把全A股基本资料全部收录。"""
    import hashlib
    import json
    from .database import validate_market_code

    payload = {key: snapshot[key] for key in ("source", "trade_date", "codes", "weights")}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                            separators=(",", ":")).encode("utf-8")).hexdigest()
    if fingerprint != snapshot.get("sha256"):
        raise ValueError("沪深300快照与固定摘要不一致，拒绝变更名单")
    members = set(snapshot["codes"])
    codes = members | {validate_market_code(code, "CN") for code in legacy}
    codes.discard("000300.SH")
    basic = {row["ts_code"]: row for row in basic_rows if row.get("ts_code") in codes}
    assets = []
    for code in sorted(codes):
        info = dict(legacy.get(code, {}))
        info.update({key: value for key, value in basic.get(code, {}).items() if value is not None and value != ""})
        assets.append({**info, "ts_code": code, "asset_type": "stock", "currency": "CNY",
                       "sources": sorted((["legacy"] if code in legacy else []) + (["hs300"] if code in members else [])),
                       "is_seed": True, "is_hs300": code in members,
                       "constituent_date": snapshot["trade_date"] if code in members else None,
                       "snapshot_sha256": snapshot["sha256"] if code in members else None,
                       "metadata_status": "complete" if info.get("name") and info.get("market") else "missing"})
    assets.append({"ts_code": "000300.SH", "name": "沪深300", "market": "指数", "asset_type": "index",
                   "currency": "CNY", "sources": ["reference"], "is_seed": False, "is_hs300": False,
                   "metadata_status": "complete"})
    return assets


def register_cn_assets(conn, assets: list[dict], *, refresh_constituents: bool = False) -> None:
    """由调用者事务发布名单；重复收录幂等，成分刷新只更新标记，不删旧股或个人数据。"""
    import json
    from .database import get_market, validate_market_code

    if get_market() != "CN":
        raise ValueError("CN资产名单只允许写入CN工作空间")
    codes = [validate_market_code(row["ts_code"], "CN") for row in assets]
    if len(set(codes)) != len(codes):
        raise ValueError("CN资产名单存在重复代码")
    for row in assets:
        if row.get("asset_type") not in {"stock", "index"}:
            raise ValueError("CN资产类型必须由元数据明确指定")
        if row["ts_code"] == "000300.SH" and row["asset_type"] != "index":
            raise ValueError("沪深300指数不能当作普通股票收录")
    if refresh_constituents:
        members = [row for row in assets if row.get("is_hs300")]
        if len(members) != 300 or len({row.get("snapshot_sha256") for row in members}) != 1:
            raise ValueError("沪深300成分刷新必须使用完整同一快照")
        conn.execute("UPDATE cn_asset_metadata SET is_hs300=0,constituent_date=NULL,snapshot_sha256=NULL")
    basic_columns = ("ts_code", "name", "area", "industry", "market", "list_date", "is_hs")
    updates = ",".join(f"{key}=COALESCE(NULLIF(excluded.{key},''),stock_basic.{key})" for key in basic_columns[1:])
    for row in assets:
        code = row["ts_code"]
        old = conn.execute("SELECT * FROM cn_asset_metadata WHERE ts_code=?", (code,)).fetchone()
        old = dict(old) if old else {}
        sources = sorted(set(json.loads(old.get("sources", "[]"))) | set(row.get("sources", [])))
        conn.execute(f"INSERT INTO stock_basic({','.join(basic_columns)}) VALUES (?,?,?,?,?,?,?) "
                     f"ON CONFLICT(ts_code) DO UPDATE SET {updates}", tuple(row.get(key) for key in basic_columns))
        seed = bool(old.get("is_seed")) or (not refresh_constituents and bool(row.get("is_seed")))
        status = row.get("metadata_status", "complete" if row.get("name") and row.get("market") else "missing")
        if old.get("metadata_status") == "complete" and status == "missing":
            status = "complete"
        conn.execute("INSERT INTO cn_asset_metadata(ts_code,asset_type,currency,sources,is_seed,is_hs300,"
                     "constituent_date,snapshot_sha256,list_status,metadata_status) VALUES (?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(ts_code) DO UPDATE SET asset_type=excluded.asset_type,currency=excluded.currency,"
                     "sources=excluded.sources,is_seed=excluded.is_seed,is_hs300=excluded.is_hs300,"
                     "constituent_date=excluded.constituent_date,snapshot_sha256=excluded.snapshot_sha256,"
                     "list_status=excluded.list_status,metadata_status=excluded.metadata_status",
                     (code, row["asset_type"], row.get("currency", "CNY"), json.dumps(sources, ensure_ascii=False),
                      int(seed), int(row.get("is_hs300", old.get("is_hs300", 0))),
                      row.get("constituent_date", old.get("constituent_date")),
                      row.get("snapshot_sha256", old.get("snapshot_sha256")), row.get("list_status", old.get("list_status")), status))


def collect_cn_sync_codes(conn) -> list[str]:
    """当前已收录个股集合包含后续手动添加项，指数按元数据进入独立链路。"""
    from .database import get_market, validate_market_code

    if get_market() != "CN":
        raise ValueError("CN同步名单只能从CN工作空间读取")
    rows = conn.execute("SELECT ts_code FROM stock_basic UNION SELECT ts_code FROM daily_kline "
                        "UNION SELECT ts_code FROM cn_asset_metadata").fetchall()
    indexes = {row[0] for row in conn.execute("SELECT ts_code FROM cn_asset_metadata WHERE asset_type='index'")}
    return sorted({validate_market_code(row[0], "CN") for row in rows if row[0] not in indexes})


CN_PRICE_BASIS_VERSION = "tushare-raw-v1:shares-cny:qfq-confirmed-close"
# index_daily官方接口单位独立核实：手/千元；点位不复权（doc_id=95）。
CN_INDEX_BASIS_VERSION = "tushare-index_daily-v1:shares-cny:points"


def _cn_source_records(frame, code, fields, label):
    """源端结构与日期必须完整明确；不接受已适配行以免重复换算。"""
    import pandas as pd
    import re

    if (not isinstance(frame, pd.DataFrame) or not {"ts_code", "trade_date", *fields}.issubset(frame.columns)
            or "basis_version" in frame.columns):
        raise ValueError(f"CN{label}源数据结构或口径无效")
    result = {}
    for row in frame.to_dict("records"):
        date = row["trade_date"]
        if row["ts_code"] != code or not isinstance(date, str) or not re.fullmatch(r"\d{8}", date):
            raise ValueError(f"CN{label}代码或日期无效")
        datetime.strptime(date, "%Y%m%d")
        if date in result:
            raise ValueError(f"CN{label}存在重复日期")
        result[date] = row
    return dict(sorted(result.items()))


def build_cn_daily_candidate(code, daily, factors=None, *, asset_type="stock"):
    """原始源数据只适配一次；个股按已确认行情复权，指数点位保持原样。"""
    import math
    from .database import validate_market_code
    from .indicators.core import DailyData

    validate_market_code(code, "CN")
    if asset_type not in {"stock", "index"} or (code == "000300.SH" and asset_type != "index"):
        raise ValueError("CN资产类型必须明确，不能把指数当个股复权")
    fields = ("open", "high", "low", "close", "vol", "amount", "pct_chg")
    raw = _cn_source_records(daily, code, fields, "日线")
    if not raw:
        raise ValueError("CN未返回有效日线，不能确认行情截止日")
    confirmed = {}
    if asset_type == "stock":
        factor_map = _cn_source_records(factors, code, ("adj_factor",), "因子")
        if not set(raw).issubset(factor_map):
            raise ValueError("CN个股因子未覆盖全部日线日期")
        confirmed = {date: float(factor_map[date]["adj_factor"]) for date in raw}
        if any(not math.isfinite(value) or value <= 0 for value in confirmed.values()):
            raise ValueError("CN复权因子必须为有限正数")
    anchor_date = max(raw)
    anchor_factor = confirmed.get(anchor_date)
    basis = CN_PRICE_BASIS_VERSION if asset_type == "stock" else CN_INDEX_BASIS_VERSION
    rows, bars = [], []
    for date, source in raw.items():
        values = {field: float(source[field]) for field in fields}
        if any(not math.isfinite(value) for value in values.values()):
            raise ValueError("CN日线价格或数量存在非有限值")
        if (min(values[key] for key in ("open", "high", "low", "close")) <= 0
                or values["high"] < max(values["open"], values["low"], values["close"])
                or values["low"] > min(values["open"], values["close"])
                or min(values["vol"], values["amount"]) < 0):
            raise ValueError("CN日线价格或数量无效")
        values["vol"] *= 100
        values["amount"] *= 1000
        rows.append({"ts_code": code, "trade_date": date, **values, "basis_version": basis})
        ratio = confirmed[date] / anchor_factor if asset_type == "stock" else 1
        adjusted = {**values, **{key: values[key] * ratio for key in ("open", "high", "low", "close")}}
        bars.append(DailyData(ts_code=code, trade_date=date, **adjusted,
                              prev_close=bars[-1].close if bars else adjusted["close"]))
    return {"ts_code": code, "asset_type": asset_type, "raw_rows": rows,
            "factor_rows": [{"ts_code": code, "trade_date": date, "adj_factor": value}
                            for date, value in confirmed.items()],
            "bars": bars, "anchor_date": anchor_date, "anchor_factor": anchor_factor,
            "basis_version": basis}


def _validate_cn_candidate(candidate):
    """发布前再次验证已规范化口径；校验不做单位换算或读取活动库。"""
    import math

    def require(condition):
        if not condition:
            raise ValueError("候选口径校验失败")

    try:
        code, asset_type = candidate["ts_code"], candidate["asset_type"]
        basis = {"stock": CN_PRICE_BASIS_VERSION, "index": CN_INDEX_BASIS_VERSION}[asset_type]
        bars, raw_rows, factors = candidate["bars"], candidate["raw_rows"], candidate["factor_rows"]
        dates = [bar.trade_date for bar in bars]
        require(dates and dates == sorted(set(dates)) and len(raw_rows) == len(dates))
        require(candidate["basis_version"] == basis and candidate["anchor_date"] == dates[-1])
        require([row["trade_date"] for row in raw_rows] == dates)
        factor_map = {row["trade_date"]: row["adj_factor"] for row in factors}
        if asset_type == "stock":
            require(len(factors) == len(dates) and set(factor_map) == set(dates))
            require(all(row["ts_code"] == code and math.isfinite(row["adj_factor"]) and row["adj_factor"] > 0
                        for row in factors))
            require(candidate["anchor_factor"] == factor_map[dates[-1]])
        else:
            require(not factors and candidate["anchor_factor"] is None)
        for bar, raw in zip(bars, raw_rows):
            require(bar.ts_code == raw["ts_code"] == code and raw["basis_version"] == basis)
            ratio = factor_map[bar.trade_date] / candidate["anchor_factor"] if asset_type == "stock" else 1
            for key in ("open", "high", "low", "close", "vol", "amount", "pct_chg"):
                expected = raw[key] * ratio if key in {"open", "high", "low", "close"} else raw[key]
                require(math.isfinite(expected) and math.isclose(getattr(bar, key), expected, rel_tol=1e-12, abs_tol=1e-12))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        raise ValueError("CN候选与原始行情、因子或口径不一致") from None


def cn_daily_request_start(start_date, stored_dates, *, history_repair=False):
    """日常回查五个已存交易日，历史修复保留旧最早日期。"""
    datetime.strptime(start_date, "%Y%m%d")
    dates = sorted(set(stored_dates))
    for date in dates:
        datetime.strptime(date, "%Y%m%d")
    if not dates:
        return start_date
    return min(start_date, dates[0]) if history_repair else dates[max(0, len(dates) - 5)]


def merge_cn_daily_candidate(code, daily, factors=None, *, stored_raw=(), asset_type="stock"):
    """旧依据已是股/元口径，只适配新增源数据，合并后统一锚定全部历史。"""
    from copy import deepcopy
    from .indicators.core import DailyData
    from .database import validate_market_code

    validate_market_code(code, "CN")
    basis = {"stock": CN_PRICE_BASIS_VERSION, "index": CN_INDEX_BASIS_VERSION}.get(asset_type)
    if basis is None:
        raise ValueError("CN资产类型无效")
    raw_map = {}
    for raw in deepcopy(list(stored_raw)):
        if raw["ts_code"] != code or raw["basis_version"] != basis or raw["trade_date"] in raw_map:
            raise ValueError("CN旧原始行情口径或代码无效，需要历史修复")
        raw_map[raw["trade_date"]] = raw
    fields = ("open", "high", "low", "close", "vol", "amount", "pct_chg")
    fresh = _cn_source_records(daily, code, fields, "日线")
    if fresh:
        adapted = build_cn_daily_candidate(code, daily, factors, asset_type=asset_type)
        raw_map.update({row["trade_date"]: row for row in adapted["raw_rows"]})
    if not raw_map:
        raise ValueError("CN未返回有效日线，不能确认行情截止日")
    factor_map = _cn_source_records(factors, code, ("adj_factor",), "因子") if asset_type == "stock" else {}
    if asset_type == "stock" and not set(raw_map).issubset(factor_map):
        raise ValueError("CN个股因子未覆盖全部已保存日线日期")
    anchor = max(raw_map)
    anchor_factor = float(factor_map[anchor]["adj_factor"]) if asset_type == "stock" else None
    bars, rows, confirmed = [], [], []
    for date, raw in sorted(raw_map.items()):
        datetime.strptime(date, "%Y%m%d")
        factor = float(factor_map[date]["adj_factor"]) if asset_type == "stock" else None
        if asset_type == "stock":
            confirmed.append({"ts_code": code, "trade_date": date, "adj_factor": factor})
        if asset_type == "stock" and (anchor_factor <= 0 or factor <= 0):
            raise ValueError("CN复权因子必须为有限正数")
        ratio = factor / anchor_factor if asset_type == "stock" else 1
        values = {key: raw[key] * ratio if key in {"open", "high", "low", "close"} else raw[key] for key in fields}
        bars.append(DailyData(ts_code=code, trade_date=date, **values,
                              prev_close=bars[-1].close if bars else values["close"]))
        rows.append(raw)
    candidate = {"ts_code": code, "asset_type": asset_type, "raw_rows": rows, "factor_rows": confirmed,
                 "bars": bars, "anchor_date": anchor, "anchor_factor": anchor_factor, "basis_version": basis}
    _validate_cn_candidate(candidate)
    return candidate


def _cn_history_ranges(start_date, end_date):
    """五年历史一次读取；更早历史按5000自然日切段，留在单次6000行上限内。"""
    first, end = datetime.strptime(start_date, "%Y%m%d"), datetime.strptime(end_date, "%Y%m%d")
    if first > end:
        raise ValueError("CN历史起止日期无效")
    while first <= end:
        last = min(end, first + timedelta(days=4999))
        yield first.strftime("%Y%m%d"), last.strftime("%Y%m%d")
        first = last + timedelta(days=1)


def fetch_cn_history(client, code, start_date, end_date, *, asset_type="stock", metadata=None):
    """按有界整段抓原始数据并严格核验覆盖；未知缺口不连接或写入业务库。"""
    import pandas as pd
    from .database import validate_market_code

    validate_market_code(code, "CN")
    if asset_type not in {"stock", "index"}:
        raise ValueError("CN资产类型无效")
    fields = ("open", "high", "low", "close", "vol", "amount", "pct_chg")
    all_daily, all_factors, explained = {}, {}, {}
    metadata = metadata or {}
    listed = metadata.get("list_date")
    delisted = metadata.get("delist_date") if metadata.get("list_status") == "D" else None
    for date in (listed, delisted):
        if date:
            datetime.strptime(date, "%Y%m%d")
    for first, last in _cn_history_ranges(start_date, end_date):
        calendar = client.get_trade_cal(exchange="SSE", start_date=first, end_date=last, raise_on_error=True)
        if not isinstance(calendar, pd.DataFrame) or not {"cal_date", "is_open"}.issubset(calendar.columns):
            raise ValueError("CN交易日历返回结构无效")
        expected_days = {(datetime.strptime(first, "%Y%m%d") + timedelta(days=n)).strftime("%Y%m%d")
                         for n in range((datetime.strptime(last, "%Y%m%d") - datetime.strptime(first, "%Y%m%d")).days + 1)}
        if (len(calendar) != len(expected_days) or set(calendar.cal_date) != expected_days
                or not calendar.is_open.isin([0, 1]).all()):
            raise ValueError("CN交易日历截断或日期无效")
        open_days = set(calendar.loc[calendar.is_open == 1, "cal_date"])
        active = {day for day in open_days if (not listed or day >= listed) and (not delisted or day <= delisted)}
        for day in open_days - active:
            explained[day] = "上市前" if listed and day < listed else "退市后"
        frame = (client.get_raw_daily(code, first, last) if asset_type == "stock" else
                 client.get_index_daily(code, first, last, raise_on_error=True))
        daily = _cn_source_records(frame, code, fields, "日线")
        if not set(daily).issubset(active):
            raise ValueError("CN日线超出请求范围、交易日历或上市边界")
        if asset_type == "stock":
            factors = _cn_source_records(client.get_adj_factor(code, first, last), code, ("adj_factor",), "因子")
            if not set(factors).issubset(expected_days) or not set(daily).issubset(factors):
                raise ValueError("CN因子截断或未覆盖日线日期")
            all_factors.update(factors)
        missing = active - set(daily)
        if missing and asset_type == "stock":
            suspension = client.get_suspend_d(code, first, last)
            stops = _cn_source_records(suspension, code, ("suspend_type", "suspend_timing"), "停牌")
            full_day = {day for day, row in stops.items() if row["suspend_type"] == "S"
                        and (pd.isna(row["suspend_timing"]) or row["suspend_timing"] == "")}
            for day in missing & full_day:
                explained[day] = "源端确认全天停牌"
            missing -= full_day
        if missing:
            raise ValueError(f"CN历史存在无法解释的行情缺口：{len(missing)}日")
        all_daily.update(daily)
    daily_frame = pd.DataFrame(list(all_daily.values()), columns=["ts_code", "trade_date", *fields])
    factor_frame = pd.DataFrame(list(all_factors.values()), columns=["ts_code", "trade_date", "adj_factor"])
    # 数值与前复权锚也属于核心校验；完全没有行情留给调用方判断身份和可用状态。
    if all_daily:
        build_cn_daily_candidate(code, daily_frame, factor_frame, asset_type=asset_type)
    coverage = {"complete": True, "start_date": start_date, "end_date": end_date,
                "actual_last_date": max(all_daily, default=None), "explained_missing": sorted(explained),
                "missing_reasons": explained, "rows": len(all_daily)}
    return daily_frame, factor_frame, coverage


def _cn_unchanged_prefix(conn, candidate):
    """只有全部已发布依据与旧指标一致时允许纯尾部增量，否则覆盖重建。"""
    code = candidate["ts_code"]
    prices = ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg")
    old_bars = [dict(row) for row in conn.execute(
        f"SELECT {','.join(prices)} FROM daily_kline WHERE ts_code=? ORDER BY trade_date", (code,))]
    old_dates = [row["trade_date"] for row in old_bars]
    if not old_dates or old_dates != [bar.trade_date for bar in candidate["bars"]][:len(old_dates)]:
        return None
    publication = conn.execute("SELECT * FROM cn_daily_publication WHERE ts_code=?", (code,)).fetchone()
    if (not publication or publication["asset_type"] != candidate["asset_type"]
            or publication["basis_version"] != candidate["basis_version"]
            or publication["anchor_factor"] != candidate["anchor_factor"]
            or publication["anchor_date"] != old_dates[-1]):
        return None
    for table, columns, expected in (
        ("daily_kline", prices, [vars(bar) for bar in candidate["bars"]][:len(old_dates)]),
        ("cn_raw_daily", (*prices, "basis_version"), candidate["raw_rows"][:len(old_dates)]),
        ("cn_adj_factor", ("ts_code", "trade_date", "adj_factor"),
         [row for row in candidate["factor_rows"] if row["trade_date"] in set(old_dates)]),
    ):
        actual = [tuple(row) for row in conn.execute(
            f"SELECT {','.join(columns)} FROM {table} WHERE ts_code=? ORDER BY trade_date", (code,))]
        if actual != [tuple(row[key] for key in columns) for row in expected]:
            return None
    indicators = [dict(row) for row in conn.execute(
        "SELECT * FROM indicator_cache WHERE ts_code=? ORDER BY trade_date", (code,))]
    base = tuple(key for key in prices if key != "amount")
    if len(indicators) != len(old_bars) or any(
            tuple(row[key] for key in base) != tuple(bar[key] for key in base)
            for row, bar in zip(indicators, old_bars)):
        return None
    return indicators


def publish_cn_daily_candidate(candidate, expected_version, *, lease=None, force_rebuild=False, progress_callback=None):
    """单标的候选在锁内计算、短事务原子发布；旧日期未覆盖则拒绝。"""
    from uuid import uuid4
    from contextlib import nullcontext
    from . import data_freshness
    from .database import get_market, market_context, validate_market_code, read_snapshot
    from .market_sync_lock import MarketSyncLease
    from .indicators import cache_builder, data_layer

    if get_market() != "CN":
        raise ValueError("CN候选只能发布到CN工作空间")
    from copy import deepcopy
    candidate = deepcopy(candidate)
    code = validate_market_code(candidate["ts_code"], "CN")
    _validate_cn_candidate(candidate)
    if lease is not None:
        if not isinstance(lease, MarketSyncLease):
            raise ValueError("行情任务锁身份无效")
        lease.validate_current_database()
    with market_context("CN", get_db_path()), nullcontext(lease) if lease is not None else MarketSyncLease.acquire(uuid4().hex):
        dates = {bar.trade_date for bar in candidate["bars"]}
        with read_snapshot() as old:
            if data_freshness._version(old, code) != expected_version:
                raise RuntimeError("CN计算期间数据版本已改变，请重新生成候选")
            old_dates = {row[0] for row in old.execute("SELECT trade_date FROM daily_kline WHERE ts_code=?", (code,))}
            if not old_dates.issubset(dates):
                raise ValueError("CN候选未覆盖旧历史，禁止删除已有行情")
            prefix = None if force_rebuild else _cn_unchanged_prefix(old, candidate)
        targets = dates - old_dates if prefix is not None else None
        if targets == set():
            return 0
        rows = cache_builder.build_indicator_candidates(candidate["bars"], dates=targets)
        if {row["trade_date"] for row in rows} != (dates if targets is None else targets):
            raise ValueError("CN候选行情、因子与指标日期不一致")
        expected_indicators = rows if prefix is None else [*prefix, *rows]
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if data_freshness._version(conn, code) != expected_version:
                raise RuntimeError("CN计算期间数据版本已改变，请重新生成候选")
            asset = conn.execute("SELECT asset_type FROM cn_asset_metadata WHERE ts_code=?", (code,)).fetchone()
            if not asset or asset[0] != candidate["asset_type"]:
                raise ValueError("CN候选资产类型与已登记元数据不一致")
            old_dates = {row[0] for row in conn.execute("SELECT trade_date FROM daily_kline WHERE ts_code=?", (code,))}
            if not old_dates.issubset(dates):
                raise ValueError("CN候选未覆盖旧历史，禁止删除已有行情")
            if prefix is None:
                for table in ("cn_raw_daily", "cn_adj_factor", "daily_kline", "indicator_cache"):
                    conn.execute(f"DELETE FROM {table} WHERE ts_code=?", (code,))
            for raw in candidate["raw_rows"]:
                if targets is not None and raw["trade_date"] not in targets:
                    continue
                columns = ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg", "basis_version")
                conn.execute(f"INSERT INTO cn_raw_daily({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                             tuple(raw[key] for key in columns))
            conn.executemany("INSERT INTO cn_adj_factor(ts_code,trade_date,adj_factor) VALUES (?,?,?)",
                             [(row["ts_code"], row["trade_date"], row["adj_factor"]) for row in candidate["factor_rows"]
                              if targets is None or row["trade_date"] in targets])
            for bar in candidate["bars"]:
                if targets is not None and bar.trade_date not in targets:
                    continue
                columns = ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg")
                conn.execute(f"INSERT INTO daily_kline({','.join(columns)},is_limit_up,is_limit_down) "
                             f"VALUES ({','.join('?' for _ in columns)},NULL,NULL)",
                             tuple(getattr(bar, key) for key in columns))
            cache_builder.persist_indicator_rows(conn, rows)
            # 核对实际落库值，不能用候选内存集合替代触发器等作用后的结果。
            price_columns = ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg")
            checks = (
                ("cn_raw_daily", (*price_columns, "basis_version"), candidate["raw_rows"]),
                ("cn_adj_factor", ("ts_code", "trade_date", "adj_factor"), candidate["factor_rows"]),
                ("daily_kline", price_columns, [vars(bar) for bar in candidate["bars"]]),
                ("indicator_cache", tuple(rows[0]), expected_indicators),
            )
            for table, columns, expected_rows in checks:
                actual = [tuple(row) for row in conn.execute(
                    f"SELECT {','.join(columns)} FROM {table} WHERE ts_code=? ORDER BY trade_date", (code,))]
                expected = [tuple(row[key] for key in columns)
                            for row in sorted(expected_rows, key=lambda row: row["trade_date"])]
                if actual != expected:
                    raise RuntimeError(f"CN事务内{table}校验失败")
            conn.execute("INSERT INTO cn_daily_publication(ts_code,asset_type,basis_version,anchor_date,anchor_factor) "
                         "VALUES (?,?,?,?,?) ON CONFLICT(ts_code) DO UPDATE SET asset_type=excluded.asset_type,"
                         "basis_version=excluded.basis_version,anchor_date=excluded.anchor_date,"
                         "anchor_factor=excluded.anchor_factor,published_at=CURRENT_TIMESTAMP",
                         (code, candidate["asset_type"], candidate["basis_version"],
                          candidate["anchor_date"], candidate["anchor_factor"]))
            publication = conn.execute(
                "SELECT asset_type,basis_version,anchor_date,anchor_factor FROM cn_daily_publication WHERE ts_code=?",
                (code,),
            ).fetchone()
            if publication is None or tuple(publication) != (
                    candidate["asset_type"], candidate["basis_version"], candidate["anchor_date"], candidate["anchor_factor"]):
                raise RuntimeError("CN事务内发布口径与锚校验失败")
            data_freshness.record_data_change(conn, code, candidate["anchor_date"])
            if progress_callback is not None:
                progress_callback(conn, candidate)
        data_layer.clear_indicator_memory_cache(code)
        return len(rows)


def sync_cn_daily(client, code, start_date, end_date, *, lease=None, history_repair=False,
                  metadata=None, progress_callback=None):
    """锁覆盖下载和计算，发布时借用同库锁；新标的和历史修复先验证完整覆盖。"""
    import pandas as pd
    from uuid import uuid4
    from contextlib import nullcontext
    from .database import get_market, market_context, read_snapshot, validate_market_code
    from .data_freshness import get_data_version
    from .market_sync_lock import MarketSyncLease

    if get_market() != "CN":
        raise ValueError("CN同步只能在CN工作空间执行")
    validate_market_code(code, "CN")
    if lease is not None:
        if not isinstance(lease, MarketSyncLease):
            raise ValueError("行情任务锁身份无效")
        lease.validate_current_database()
    lock = nullcontext(lease) if lease is not None else MarketSyncLease.acquire(uuid4().hex)
    with market_context("CN", get_db_path()), lock as held:
        with read_snapshot() as conn:
            asset = conn.execute("SELECT * FROM cn_asset_metadata WHERE ts_code=?", (code,)).fetchone()
            basic = conn.execute("SELECT * FROM stock_basic WHERE ts_code=?", (code,)).fetchone()
            if not asset:
                raise ValueError("CN同步标的尚未登记明确资产类型")
            info = {**(dict(basic) if basic else {}), **dict(asset), **(metadata or {})}
            if info.get("ts_code", code) != code or info["asset_type"] != asset["asset_type"]:
                raise ValueError("CN同步元数据与已登记标的身份不一致")
            version = get_data_version(code)
            stored_dates = [row[0] for row in conn.execute(
                "SELECT trade_date FROM daily_kline WHERE ts_code=? ORDER BY trade_date", (code,))]
            old_raw = [dict(row) for row in conn.execute(
                "SELECT * FROM cn_raw_daily WHERE ts_code=? ORDER BY trade_date", (code,))]
        first = min(start_date, stored_dates[0]) if stored_dates else start_date
        complete_raw = {row["trade_date"] for row in old_raw} == set(stored_dates)
        repair = history_repair or not complete_raw
        request_start = cn_daily_request_start(first, stored_dates, history_repair=repair)
        daily, factors, coverage = fetch_cn_history(client, code, request_start, end_date,
                                                    asset_type=asset["asset_type"], metadata=info)
        # 因子历史必须核对全部保存日期，不能仅比较五日重叠窗口或最后一个因子。
        if asset["asset_type"] == "stock" and first < request_start:
            chunks = []
            before = (datetime.strptime(request_start, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            for begin, finish in _cn_history_ranges(first, before):
                frame = client.get_adj_factor(code, begin, finish)
                records = _cn_source_records(frame, code, ("adj_factor",), "因子")
                if any(not begin <= date <= finish for date in records):
                    raise ValueError("CN因子历史超出请求范围")
                chunks.append(frame)
            factors = pd.concat([*chunks, factors], ignore_index=True)
        candidate = merge_cn_daily_candidate(code, daily, factors,
                                             stored_raw=() if repair else old_raw, asset_type=asset["asset_type"])
        written = publish_cn_daily_candidate(candidate, version, lease=held, force_rebuild=repair,
                                              progress_callback=progress_callback)
        return {"ts_code": code, "new_rows": len({bar.trade_date for bar in candidate["bars"]} - set(stored_dates)),
                "indicator_rows": written, "actual_last_date": candidate["anchor_date"], "coverage": coverage}


def sync_indicator_cache_incremental(ts_code: str) -> int:
    """仅补缺失日期，无需初始化任何行情客户端；失败向调用方传播。"""
    from .indicators.cache_builder import write_indicator_rows

    return write_indicator_rows(ts_code)


def sync_workspace_stocks(ts_codes: list[str], days: int = 730, *, mode: str = "daily_and_compute") -> dict[str, int]:
    """工作空间脚本入口：固定库、同库写锁及备份，US使用Yahoo和离线指标。"""
    from contextlib import ExitStack, closing
    from uuid import uuid4
    from api.services import sync_service
    from .database import get_market, market_context, validate_market_code
    from .data_freshness import data_update_batch
    from .market_sync_lock import MarketSyncLease
    from . import yahoo_sync

    if mode not in {"missing", "daily", "daily_and_compute", "indicators"} or days < 1:
        raise ValueError("同步模式或天数无效")
    with market_context(get_market(), get_db_path()) as binding:
        codes = list(dict.fromkeys(validate_market_code(code) for code in ts_codes))
        if binding.market == "CN":
            raise ValueError("CN行情与指标事务发布适配尚未就绪，不能使用旧同步入口")
        if not codes:
            return {}
        with ExitStack() as resources:
            task_id = uuid4().hex
            resources.enter_context(MarketSyncLease.acquire(task_id))
            if mode == "missing":
                with get_connection() as conn:
                    have = {row[0] for row in conn.execute("SELECT DISTINCT ts_code FROM daily_kline")}
                codes = [code for code in codes if code not in have]
            if not codes:
                return {}
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            sync_service.backup_database(binding.db_path.parent / "backups" / f"US-pre-script-{stamp}-{task_id}.db")
            resources.enter_context(data_update_batch())
            session = None
            now = sync_service._beijing_now()
            start = (now - timedelta(days=days)).strftime("%Y%m%d")
            with get_connection() as conn:
                latest = dict(conn.execute("SELECT ts_code, MAX(trade_date) FROM daily_kline GROUP BY ts_code"))
            results = {}
            for code in codes:
                if mode == "indicators":
                    results[code] = sync_indicator_cache_incremental(code)
                    continue
                end = sync_service.compute_market_end_date(now, sync_service._market_of(code))
                first = start
                if latest.get(code):
                    first = max(start, (datetime.strptime(latest[code], "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d"))
                added = 0
                if first <= end:
                    if session is None:
                        session = resources.enter_context(closing(yahoo_sync._default_session()))
                    added = yahoo_sync.sync_us_daily(code, first, end, session=session, incremental=True)
                    if not added:
                        raise RuntimeError(f"{code} 数据源未返回新行情，不能确认已更新到 {end}")
                results[code] = added
                if mode == "daily_and_compute":
                    sync_indicator_cache_incremental(code)
            return results


def run_workspace_sync_command(action, *, ts_code=None, days=730, indicators=False, skip_indicators=False):
    """统一CLI与旧模块入口，避免默认US调用Tushare或导入全A股。"""
    from .database import get_market, init_database, validate_market_code
    from .data_freshness import get_data_status

    if action == "status":
        status = get_data_status()
        print(f"工作空间 {get_market()} · 数据库: {get_db_path()}")
        print(status)
        return status
    if ts_code:
        validate_market_code(ts_code)
    if get_market() == "CN":
        raise ValueError("CN安全初始化与行情事务发布尚未就绪，请勿使用旧初始化/同步命令")
    if action == "init":
        init_database()
        return
    if action == "stk-factor":
        raise ValueError("US工作空间不适用Tushare官方A股指标")
    if action != "sync":
        raise ValueError("同步操作无效")
    from api.services.sync_service import collect_us_hk_codes

    codes = [ts_code] if ts_code else collect_us_hk_codes()
    mode = "daily_and_compute" if not skip_indicators and (ts_code or indicators) else "daily"
    results = sync_workspace_stocks(codes, days=days, mode=mode)
    print(f"{get_market()} 同步结束: {results}")
    return results


def _disabled_legacy_source_write(method):
    """保留旧签名供兼容检测，在任何源请求、读库或写库前明确拒绝。"""
    from functools import wraps

    @wraps(method)
    def disabled(*args, **kwargs):
        raise ValueError("旧Tushare写入口已禁用：US请使用Yahoo工作空间同步，CN请使用安全初始化与原子发布管线")

    return disabled


def _bound_us_local_indicators(method):
    """旧离线指标只允许US；实例和线程始终使用创建时固定的库。"""
    from functools import wraps

    @wraps(method)
    def bound(self, *args, **kwargs):
        from .database import get_market, market_context, validate_market_code

        if get_market() == "CN":
            raise ValueError("CN指标必须通过原子行情发布，不能使用旧离线写入口")
        codes = args[0] if args else kwargs.get("ts_code", kwargs.get("ts_codes"))
        for code in ([codes] if isinstance(codes, str) else codes or []):
            validate_market_code(code, "US")
        with market_context(self.binding.market, self.binding.db_path):
            return method(self, *args, **kwargs)

    return bound


class DataSyncer:
    """US离线指标兼容入口；旧Tushare写入口不可执行。"""

    def __init__(self, token: str | None = None):
        from .database import get_market, market_context

        if get_market() == "CN":
            raise ValueError("CN指标必须通过原子行情发布，不能初始化旧同步器")
        with market_context("US", get_db_path()) as binding:
            self.binding = binding
        self.token = token
        self.pro = None

        # 向后兼容：保留 instance-level attrs（外部可能引用）
        # 但实际限流走模块级 _GLOBAL_LIMITER
        # （v2.11.0 计划移除，改用 @property + DeprecationWarning）
        self.last_request_time: dict[str, float] = {}

    def _rate_limit(self, api_name: str):
        """线程安全的限流控制（v2.10.0 P1-4 改为调模块级 _GLOBAL_LIMITER）"""
        # v2.10.0：原 per-instance lock 改用模块级 multiprocessing 安全限流器
        _rate_limit_global()
        # 保留旧字段更新，便于外部观察（不影响实际限流）
        self.last_request_time[api_name] = time.time()

    def _log_sync(self, data_type: str, ts_code: str | None, last_date: str, status: str, message: str = ""):
        """记录同步日志"""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sync_log (data_type, ts_code, last_date, status, message)
                VALUES (?, ?, ?, ?, ?)
            """,
                (data_type, ts_code, last_date, status, message),
            )

    def _get_last_date(self, data_type: str, ts_code: str | None = None) -> str | None:
        """获取最后同步日期"""
        with get_connection() as conn:
            cursor = conn.cursor()
            if ts_code:
                cursor.execute(
                    """
                    SELECT last_date FROM sync_log
                    WHERE data_type = ? AND ts_code = ? AND status = 'success'
                    ORDER BY created_at DESC LIMIT 1
                """,
                    (data_type, ts_code),
                )
            else:
                cursor.execute(
                    """
                    SELECT last_date FROM sync_log
                    WHERE data_type = ? AND ts_code IS NULL AND status = 'success'
                    ORDER BY created_at DESC LIMIT 1
                """,
                    (data_type,),
                )
            result = cursor.fetchone()
            return result["last_date"] if result else None

    # ==================== 股票基本信息 ====================

    @_disabled_legacy_source_write
    def sync_stock_basic(self) -> int:
        """
        同步股票基本信息
        股票信息基本不变化，每周同步一次即可
        """
        logger.info("开始同步股票基本信息...")
        try:
            self._rate_limit("stock_basic")
            df = self.pro.stock_basic(
                exchange="", list_status="L", fields="ts_code,name,area,industry,market,list_date,is_hs"
            )

            if df is None or len(df) == 0:
                logger.warning("获取股票基本信息失败")
                return 0

            # 填充 NaN 以免插入失败，且保留必要的列
            df = df[["ts_code", "name", "area", "industry", "market", "list_date", "is_hs"]].fillna("")
            with get_connection() as conn:
                df.to_sql("stock_basic", conn, if_exists="append", index=False, method="multi")

            self._log_sync("stock_basic", None, datetime.now().strftime("%Y%m%d"), "success")
            logger.info(f"股票基本信息同步完成，共 {len(df)} 只")
            return len(df)

        except Exception as e:
            logger.error(f"股票基本信息同步失败: {e}")
            self._log_sync("stock_basic", None, "", "failed", str(e))
            return 0

    # ==================== 日线K线数据 ====================

    @_disabled_legacy_source_write
    def sync_daily_kline(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        raise_on_error: bool = False,
    ) -> int:
        """
        同步单只股票的日线数据（增量更新）

        Args:
            ts_code: 股票代码，如 '000001.SZ'
            start_date: 开始日期，格式 YYYYMMDD，None 表示从数据库最后一条开始
            end_date: 结束日期，格式 YYYYMMDD，None 表示到最新
            raise_on_error: True 时异常透传（调用方可区分失败与无新增）；
                默认 False 保持旧行为：记录日志并返回 0

        Returns:
            更新条数
        """
        # 增量更新：获取最后同步日期
        if start_date is None:
            last_date = self._get_last_date("daily_kline", ts_code)
            if last_date:
                # 从后一天开始
                last_dt = datetime.strptime(last_date, "%Y%m%d")
                start_date = (last_dt + timedelta(days=1)).strftime("%Y%m%d")

        # 默认从2年前开始
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=730)).strftime("%Y%m%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        try:
            self._rate_limit("daily_kline")
            df = ts.pro_bar(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
                adj="qfq",
                api=self.pro,
            )

            if df is None or len(df) == 0:
                return 0

            # 计算量比（需要历史数据，这里先跳过，由指标计算模块处理）
            # 计算涨跌停标记
            df["is_limit_up"] = df["pct_chg"].apply(lambda x: 1 if x >= _LIMIT_THRESHOLD else 0)
            df["is_limit_down"] = df["pct_chg"].apply(lambda x: 1 if x <= -_LIMIT_THRESHOLD else 0)

            with get_connection() as conn:
                cursor = conn.cursor()

                # 准备批量插入的数据
                records = []
                for row in df.itertuples(index=False):
                    row_dict = row._asdict()
                    records.append(
                        (
                            row_dict["ts_code"],
                            row_dict["trade_date"],
                            row_dict["open"],
                            row_dict["high"],
                            row_dict["low"],
                            row_dict["close"],
                            row_dict["vol"],
                            row_dict["amount"],
                            row_dict.get("pct_chg", 0),
                            None,  # vol_ratio later
                            row_dict.get("is_limit_up", 0),
                            row_dict.get("is_limit_down", 0),
                        )
                    )

                cursor.executemany(
                    """
                    INSERT OR REPLACE INTO daily_kline
                    (ts_code, trade_date, open, high, low, close, vol, amount,
                     pct_chg, vol_ratio, is_limit_up, is_limit_down)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    records,
                )

            # 更新同步日志
            latest_date = df["trade_date"].max()
            self._log_sync("daily_kline", ts_code, latest_date, "success")

            logger.info(f"日线数据同步完成: {ts_code}, {len(df)} 条, {start_date}-{latest_date}")
            return len(df)

        except Exception as e:
            logger.error(f"日线数据同步失败 {ts_code}: {e}")
            self._log_sync("daily_kline", ts_code, "", "failed", str(e))
            if raise_on_error:
                raise
            return 0

    @_disabled_legacy_source_write
    def sync_missing(self, ts_codes: list[str], days: int = 730) -> dict[str, int]:
        """
        同步 ts_codes 中"在 daily_kline 表里完全缺失"的股票（增量补齐）

        与 sync_all_daily_kline 的区别：
        - sync_all_daily_kline：所有 ts_codes 都同步（已有的会跳过早于 2 天的部分）
        - sync_missing：只在 daily_kline 表里完全没有数据的才同步

        用于"自选股清单第一次接入"或"补齐漏掉的股票"场景

        Args:
            ts_codes: 股票代码列表
            days: 同步天数

        Returns:
            每只股票的更新条数
        """
        if not ts_codes:
            return {}

        with get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join(["?"] * len(ts_codes))
            cursor.execute(
                f"SELECT DISTINCT ts_code FROM daily_kline WHERE ts_code IN ({placeholders})",
                ts_codes,
            )
            have = {row["ts_code"] for row in cursor.fetchall()}

        missing = [c for c in ts_codes if c not in have]
        logger.info(f"sync_missing: 共 {len(ts_codes)} 只，已有 {len(have)} 只，需补齐 {len(missing)} 只")

        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        results = {}
        for code in missing:
            count = self.sync_daily_kline(code, start_date=start_date)
            results[code] = count
        return results

    @_disabled_legacy_source_write
    def sync_all_daily_kline(self, ts_codes: list[str] | None = None, days: int = 730) -> dict[str, int]:
        """
        批量同步多只股票的日线数据（并发执行）

        Args:
            ts_codes: 股票代码列表，None 表示同步所有股票
            days: 同步天数，默认2年

        Returns:
            每只股票的更新条数
        """
        results = {}

        # 如果没有指定股票，获取所有股票代码
        if ts_codes is None:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT ts_code FROM stock_basic")
                ts_codes = [row["ts_code"] for row in cursor.fetchall()]

        logger.info(f"开始批量同步日线数据，共 {len(ts_codes)} 只股票...")

        # 计算起始日期
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")

        # 进度追踪锁
        progress_lock = threading.Lock()
        completed = 0
        total = len(ts_codes)

        def sync_single(ts_code):
            nonlocal completed
            try:
                # 检查是否已有数据，避免重复同步
                last_date = self._get_last_date("daily_kline", ts_code)
                if last_date:
                    last_dt = datetime.strptime(last_date, "%Y%m%d")
                    if (datetime.now() - last_dt).days < 2:
                        with progress_lock:
                            completed += 1
                        return ts_code, 0  # Skip

                count = self.sync_daily_kline(ts_code, start_date, end_date)

                with progress_lock:
                    completed += 1
                    if completed % 10 == 0:
                        logger.info(f"进度: {completed}/{total}")

                return ts_code, count
            except Exception as e:
                logger.error(f"同步失败 {ts_code}: {e}")
                with progress_lock:
                    completed += 1
                return ts_code, 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_SYNC_WORKERS) as executor:
            futures = [executor.submit(sync_single, code) for code in ts_codes]
            for future in concurrent.futures.as_completed(futures):
                code, count = future.result()
                results[code] = count

        logger.info(f"批量同步完成，成功 {sum(1 for v in results.values() if v > 0)}/{len(ts_codes)}")
        return results

    # ==================== 指标缓存 ====================

    @_bound_us_local_indicators
    def sync_indicator_cache(self, ts_code: str, days: int = 120) -> int:
        """
        同步单只股票的技术指标到 indicator_cache 表

        Args:
            ts_code: 股票代码
            days: 计算天数

        Returns:
            更新条数
        """
        try:
            # 导入指标计算模块
            from .indicators import (
                get_kline_data,
                precompute_kdj_sequence,
                precompute_macd_sequence,
                calculate_bbi,
                calculate_ma,
                calculate_rsi_multi,
                calculate_wr_multi,
                calculate_bollinger,
                calculate_vol_ratio,
                calculate_zg_white,
                calculate_dg_yellow,
                detect_needle_20,
                calculate_brick_value,
                detect_brick_trend,
                detect_fanbao,
                detect_volume_pattern,
                calculate_sell_score,
                detect_trade_signal,
                calculate_dmi,
            )

            # 获取K线数据
            klines = get_kline_data(ts_code, days)
            if not klines:
                return 0

            # 预计算指标序列（避免循环中O(n²)重复计算）
            kdj_seq = precompute_kdj_sequence(klines) if len(klines) >= 9 else None
            macd_dif_seq, macd_dea_seq, macd_hist_seq = (
                precompute_macd_sequence(klines) if len(klines) >= 30 else (None, None, None)
            )

            # 准备写入数据
            with get_connection() as conn:
                cursor = conn.cursor()

                prev_white, prev_yellow = 0, 0
                prev_brick = 0
                prev_brick_color = None
                consecutive_bricks = 0
                for i, kline in enumerate(klines):
                    # 计算单日指标
                    sub_klines = klines[: i + 1]
                    today = kline
                    yesterday = sub_klines[-2] if len(sub_klines) > 1 else None

                    # 获取各项指标（优先从预计算序列取值）
                    if kdj_seq:
                        k, d, j = kdj_seq[i]
                    else:
                        k, d, j = 50, 50, 50

                    if macd_dif_seq is not None and macd_dea_seq is not None and macd_hist_seq is not None:
                        dif = macd_dif_seq[i]
                        dea = macd_dea_seq[i]
                        macd_hist = macd_hist_seq[i]
                    else:
                        dif, dea, macd_hist = 0.0, 0.0, 0.0

                    bbi = calculate_bbi(sub_klines) if len(sub_klines) >= 24 else 0

                    closes = [k.close for k in sub_klines]
                    ma5 = calculate_ma(closes, 5) if len(closes) >= 5 else 0
                    ma10 = calculate_ma(closes, 10) if len(closes) >= 10 else 0
                    ma20 = calculate_ma(closes, 20) if len(closes) >= 20 else 0
                    ma60 = calculate_ma(closes, 60) if len(closes) >= 60 else 0

                    rsi6, rsi12, rsi24 = calculate_rsi_multi(sub_klines) if len(sub_klines) >= 25 else (50, 50, 50)
                    wr5, wr10 = calculate_wr_multi(sub_klines) if len(sub_klines) >= 10 else (-50, -50)

                    boll_mid, boll_upper, boll_lower, boll_width, boll_pos = (
                        calculate_bollinger(sub_klines) if len(sub_klines) >= 20 else (0, 0, 0, 0, 50)
                    )

                    vol_ratio = calculate_vol_ratio(sub_klines)

                    zg_white = calculate_zg_white(sub_klines) if len(sub_klines) >= 115 else 0
                    dg_yellow = calculate_dg_yellow(sub_klines) if len(sub_klines) >= 115 else 0
                    # 原双线公式需至少 3 个有效点（第 114~116 根），仅比较昨日和今日。
                    gold_cross = i >= 115 and prev_white <= prev_yellow and zg_white > dg_yellow
                    dead_cross = i >= 115 and prev_white >= prev_yellow and zg_white < dg_yellow
                    prev_white, prev_yellow = zg_white, dg_yellow

                    rsl_short, rsl_long, is_needle = (
                        detect_needle_20(sub_klines) if len(sub_klines) >= 22 else (50, 50, False)
                    )

                    brick_value = calculate_brick_value(sub_klines) if len(sub_klines) >= 8 else 0
                    # 原公式从第 8 根开始比较砖值；复用逐日结果，避免三重历史遍历。
                    if i >= 8:
                        color = "RED" if brick_value >= prev_brick else "GREEN"
                        consecutive_bricks = consecutive_bricks + 1 if color == prev_brick_color else 1
                        prev_brick_color = color
                    prev_brick = brick_value
                    brick_trend, brick_count = (
                        (prev_brick_color, consecutive_bricks) if i >= 9 else ("NEUTRAL", 0)
                    )
                    brick_trend_up = detect_brick_trend(sub_klines) if len(sub_klines) >= 115 else False
                    is_fanbao = detect_fanbao(sub_klines) if len(sub_klines) >= 4 else False

                    vol_pattern = detect_volume_pattern(today, yesterday) if yesterday else {}
                    sell_result = calculate_sell_score(sub_klines) if len(sub_klines) >= 5 else (3, {})
                    sell_score = sell_result[0]
                    sell_items = sell_result[1] if isinstance(sell_result[1], dict) else {}
                    sell_reason = ",".join([k for k, v in sell_items.items() if not v]) if sell_items else "数据不足"
                    signal = detect_trade_signal(sub_klines) if len(sub_klines) >= 30 else "WATCH"
                    signal_desc = signal.value if hasattr(signal, "value") else str(signal)

                    dmi_plus, dmi_minus, adx = calculate_dmi(sub_klines) if len(sub_klines) >= 30 else (0, 0, 0)

                    # 昨高昨低
                    prev_high = sub_klines[-2].high if len(sub_klines) > 1 else 0
                    prev_low = sub_klines[-2].low if len(sub_klines) > 1 else 0

                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO indicator_cache
                        (ts_code, trade_date, close, open, high, low, vol, pct_chg,
                         k, d, j, dif, dea, macd_hist, bbi,
                         ma5, ma10, ma20, ma60,
                         rsi6, rsi12, rsi24, wr5, wr10,
                         boll_mid, boll_upper, boll_lower, boll_width, boll_position,
                         vol_ratio, zg_white, dg_yellow,
                         is_gold_cross, is_dead_cross,
                         rsl_short, rsl_long, is_needle_20,
                         brick_value, brick_trend, brick_count, brick_trend_up, is_fanbao,
                         is_beidou, is_suoliang, is_jiayin_zhenyang, is_jiayang_zhenyin, is_fangliang_yinxian,
                         sell_score, sell_reason, signal, signal_desc,
                         prev_high, prev_low, dmi_plus, dmi_minus, adx,
                         net_lg_mf, net_elg_mf, last_b1_date, last_b1_price,
                         last_yidong_date, market_pct_chg, market_dir, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            ts_code,
                            today.trade_date,
                            today.close,
                            today.open,
                            today.high,
                            today.low,
                            today.vol,
                            today.pct_chg,
                            k,
                            d,
                            j,
                            dif,
                            dea,
                            macd_hist,
                            bbi,
                            ma5,
                            ma10,
                            ma20,
                            ma60,
                            rsi6,
                            rsi12,
                            rsi24,
                            wr5,
                            wr10,
                            boll_mid,
                            boll_upper,
                            boll_lower,
                            boll_width,
                            boll_pos,
                            vol_ratio,
                            zg_white,
                            dg_yellow,
                            int(gold_cross),
                            int(dead_cross),
                            rsl_short,
                            rsl_long,
                            int(is_needle),
                            brick_value,
                            brick_trend,
                            brick_count,
                            int(brick_trend_up),
                            int(is_fanbao),
                            int(vol_pattern.get("is_beidou", 0)),
                            int(vol_pattern.get("is_suoliang", 0)),
                            int(vol_pattern.get("is_jiayin_zhenyang", 0)),
                            int(vol_pattern.get("is_jiayang_zhenyin", 0)),
                            int(vol_pattern.get("is_fangliang_yinxian", 0)),
                            sell_score,
                            sell_reason,
                            signal_desc,
                            signal_desc,
                            prev_high,
                            prev_low,
                            dmi_plus,
                            dmi_minus,
                            adx,
                            0,
                            0,
                            None,
                            0,
                            None,
                            0,
                            "NEUTRAL",
                            None,
                        ),
                    )

            self._log_sync("indicator_cache", ts_code, klines[-1].trade_date, "success")
            logger.info(f"指标缓存同步完成: {ts_code}, {len(klines)} 条")
            return len(klines)

        except Exception as e:
            logger.error(f"指标缓存同步失败 {ts_code}: {e}")
            self._log_sync("indicator_cache", ts_code, "", "failed", str(e))
            return 0

    @_bound_us_local_indicators
    def sync_all_indicators(self, ts_codes: list[str] | None = None) -> dict[str, int]:
        """
        批量同步所有股票的指标缓存（并发执行）

        Args:
            ts_codes: 股票代码列表，None 表示同步所有有K线数据的股票

        Returns:
            每只股票的更新条数
        """
        results = {}

        if ts_codes is None:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT ts_code FROM daily_kline")
                ts_codes = [row["ts_code"] for row in cursor.fetchall()]

        logger.info(f"开始批量同步指标缓存，共 {len(ts_codes)} 只股票...")

        progress_lock = threading.Lock()
        completed = 0
        total = len(ts_codes)

        def sync_single(ts_code):
            nonlocal completed
            try:
                count = self.sync_indicator_cache(ts_code)
                with progress_lock:
                    completed += 1
                    if completed % 10 == 0:
                        logger.info(f"进度: {completed}/{total}")
                return ts_code, count
            except Exception as e:
                logger.error(f"指标同步失败 {ts_code}: {e}")
                with progress_lock:
                    completed += 1
                return ts_code, 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_SYNC_WORKERS) as executor:
            futures = [executor.submit(sync_single, code) for code in ts_codes]
            for future in concurrent.futures.as_completed(futures):
                code, count = future.result()
                results[code] = count

        logger.info(f"批量指标同步完成，成功 {sum(1 for v in results.values() if v > 0)}/{len(ts_codes)}")
        return results

    @_disabled_legacy_source_write
    def sync_daily_and_compute(self, ts_codes: list[str] | None = None, days: int = 730) -> dict[str, int]:
        """
        一站式：同步日线 K 线 + 同步指标缓存

        这是 scripts/sync_and_compute.py 业务逻辑的接收方
        （v2.10.0 之前是 ~300 行的内联实现）

        Args:
            ts_codes: 股票代码列表，None = 全市场
            days: 同步天数

        Returns:
            每只股票的指标更新条数（dict[ts_code] = count）
        """
        kline_results = self.sync_all_daily_kline(ts_codes=ts_codes, days=days)
        # 同步哪些股票有数据，传给指标计算
        if ts_codes is None:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT ts_code FROM daily_kline")
                ts_codes_for_indic = [row["ts_code"] for row in cursor.fetchall()]
        else:
            ts_codes_for_indic = [c for c, n in kline_results.items() if n > 0]
        return self.sync_all_indicators(ts_codes=ts_codes_for_indic or None)

    # ==================== Tushare 官方指标（用于 diff 验证） ====================

    @_disabled_legacy_source_write
    def sync_stk_factor(self, ts_code: str, start_date: str | None = None, end_date: str | None = None) -> int:
        """
        同步单只股票的 Tushare 官方技术指标（stk_factor 接口）

        Args:
            ts_code: 股票代码
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD

        Returns:
            更新条数
        """
        try:
            if start_date is None:
                start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
            if end_date is None:
                end_date = datetime.now().strftime("%Y%m%d")

            self._rate_limit("stk_factor")
            df = self.pro.stk_factor(ts_code=ts_code, start_date=start_date, end_date=end_date)

            if df is None or len(df) == 0:
                return 0

            # 字段映射：Tushare 字段名 -> 数据库字段名
            field_map = {
                "ts_code": "ts_code",
                "trade_date": "trade_date",
                "close": "close",
                "macd_dif": "macd_dif",
                "macd_dea": "macd_dea",
                "macd": "macd",
                "kdj_k": "kdj_k",
                "kdj_d": "kdj_d",
                "kdj_j": "kdj_j",
                "rsi_6": "rsi_6",
                "rsi_12": "rsi_12",
                "rsi_24": "rsi_24",
                "boll_upper": "boll_upper",
                "boll_mid": "boll_mid",
                "boll_lower": "boll_lower",
                "cci": "cci",
            }

            with get_connection() as conn:
                cursor = conn.cursor()
                records = []
                for row in df.itertuples(index=False):
                    row_dict = row._asdict()
                    values = [row_dict.get(field_map.get(k, k), 0) for k in field_map.keys()]
                    records.append(values)

                cursor.executemany(
                    """
                    INSERT OR REPLACE INTO tushare_indicator_cache
                    (ts_code, trade_date, close, macd_dif, macd_dea, macd,
                     kdj_k, kdj_d, kdj_j, rsi_6, rsi_12, rsi_24,
                     boll_upper, boll_mid, boll_lower, cci)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    records,
                )

            latest_date = df["trade_date"].max()
            self._log_sync("stk_factor", ts_code, latest_date, "success")
            logger.info(f"Tushare 指标同步完成: {ts_code}, {len(df)} 条")
            return len(df)

        except Exception as e:
            logger.error(f"Tushare 指标同步失败 {ts_code}: {e}")
            self._log_sync("stk_factor", ts_code, "", "failed", str(e))
            return 0

    @_disabled_legacy_source_write
    def sync_all_stk_factor(self, ts_codes: list[str] | None = None, days: int = 365) -> dict[str, int]:
        """
        批量同步多只股票的 Tushare 官方指标（并发执行）

        Args:
            ts_codes: 股票代码列表，None 表示同步所有股票
            days: 同步天数

        Returns:
            每只股票的更新条数
        """
        results = {}

        if ts_codes is None:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT ts_code FROM stock_basic")
                ts_codes = [row["ts_code"] for row in cursor.fetchall()]

        logger.info(f"开始批量同步 Tushare 指标，共 {len(ts_codes)} 只股票...")

        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")

        progress_lock = threading.Lock()
        completed = 0
        total = len(ts_codes)

        def sync_single(ts_code):
            nonlocal completed
            try:
                count = self.sync_stk_factor(ts_code, start_date, end_date)
                with progress_lock:
                    completed += 1
                    if completed % 10 == 0:
                        logger.info(f"进度: {completed}/{total}")
                return ts_code, count
            except Exception as e:
                logger.error(f"Tushare 指标同步失败 {ts_code}: {e}")
                with progress_lock:
                    completed += 1
                return ts_code, 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_SYNC_WORKERS) as executor:
            futures = [executor.submit(sync_single, code) for code in ts_codes]
            for future in concurrent.futures.as_completed(futures):
                code, count = future.result()
                results[code] = count

        logger.info(f"批量 Tushare 指标同步完成，成功 {sum(1 for v in results.values() if v > 0)}/{len(ts_codes)}")
        return results

    # ==================== 每日估值指标 (PE/PB/PS) ====================

    @_disabled_legacy_source_write
    def ensure_daily_basic_columns(self):
        """确保 daily_kline 表包含 PE/PB/PS/总市值/流通市值 列"""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(daily_kline)")
            existing = {row[1] for row in cursor.fetchall()}

            for col_name, col_type in [
                ("pe", "REAL"),
                ("pe_ttm", "REAL"),
                ("pb", "REAL"),
                ("ps", "REAL"),
                ("ps_ttm", "REAL"),
                ("total_mv", "REAL"),
                ("circ_mv", "REAL"),
            ]:
                if col_name not in existing:
                    cursor.execute(f"ALTER TABLE daily_kline ADD COLUMN {col_name} {col_type}")
                    logger.info(f"Added column {col_name} to daily_kline")

    @_disabled_legacy_source_write
    def sync_daily_basic(self, ts_code: str, start_date: str = "", end_date: str = "") -> int:
        """
        同步单只股票的每日估值指标（PE/PB/PS/市值等）

        使用 Tushare daily_basic 接口，数据写入 daily_kline 表对应列。

        Args:
            ts_code: 股票代码
            start_date: 起始日期 YYYYMMDD，默认 2 年前
            end_date: 结束日期 YYYYMMDD，默认今天

        Returns:
            更新条数
        """
        try:
            self.ensure_daily_basic_columns()
            self._rate_limit("daily_basic")

            if not start_date:
                start_date = (datetime.now() - timedelta(days=730)).strftime("%Y%m%d")
            if not end_date:
                end_date = datetime.now().strftime("%Y%m%d")

            df = self.pro.daily_basic(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )

            if df is None or len(df) == 0:
                return 0

            with get_connection() as conn:
                cursor = conn.cursor()
                for row in df.itertuples(index=False):
                    row_dict = row._asdict()
                    cursor.execute(
                        """
                        UPDATE daily_kline SET
                            pe = ?, pe_ttm = ?, pb = ?, ps = ?, ps_ttm = ?,
                            total_mv = ?, circ_mv = ?
                        WHERE ts_code = ? AND trade_date = ?
                    """,
                        (
                            row_dict.get("pe"),
                            row_dict.get("pe_ttm"),
                            row_dict.get("pb"),
                            row_dict.get("ps"),
                            row_dict.get("ps_ttm"),
                            row_dict.get("total_mv"),
                            row_dict.get("circ_mv"),
                            row_dict["ts_code"],
                            row_dict["trade_date"],
                        ),
                    )

            self._log_sync("daily_basic", ts_code, end_date, "success")
            return len(df)

        except Exception as e:
            logger.error(f"每日估值指标同步失败 {ts_code}: {e}")
            self._log_sync("daily_basic", ts_code, "", "failed", str(e))
            return 0

    @_disabled_legacy_source_write
    def sync_all_daily_basic(self, ts_codes: list[str] | None = None, days: int = 730) -> dict[str, int]:
        """
        批量同步多只股票的每日估值指标（并发执行）

        Args:
            ts_codes: 股票代码列表，None 表示同步所有有 K 线的股票
            days: 同步天数，默认 2 年

        Returns:
            每只股票的更新条数
        """
        self.ensure_daily_basic_columns()
        results = {}

        if ts_codes is None:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT ts_code FROM daily_kline ORDER BY ts_code")
                ts_codes = [row[0] for row in cursor.fetchall()]

        logger.info(f"开始批量同步每日估值指标，共 {len(ts_codes)} 只股票...")

        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")

        progress_lock = threading.Lock()
        completed = 0
        total = len(ts_codes)

        def sync_single(ts_code):
            nonlocal completed
            try:
                count = self.sync_daily_basic(ts_code, start_date, end_date)
                with progress_lock:
                    completed += 1
                    if completed % 10 == 0:
                        logger.info(f"进度: {completed}/{total}")
                return ts_code, count
            except Exception as e:
                logger.error(f"估值指标同步失败 {ts_code}: {e}")
                with progress_lock:
                    completed += 1
                return ts_code, 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_SYNC_WORKERS) as executor:
            futures = [executor.submit(sync_single, code) for code in ts_codes]
            for future in concurrent.futures.as_completed(futures):
                code, count = future.result()
                results[code] = count

        logger.info(f"批量估值指标同步完成，成功 {sum(1 for v in results.values() if v > 0)}/{len(ts_codes)}")
        return results

    # ==================== 资金流向 ====================

    @_disabled_legacy_source_write
    def sync_moneyflow(self, ts_code: str, trade_date: str) -> int:
        """
        同步单只股票的单日资金流向

        Args:
            ts_code: 股票代码
            trade_date: 交易日期，格式 YYYYMMDD

        Returns:
            更新条数
        """
        try:
            self._rate_limit("moneyflow")
            df = self.pro.moneyflow(ts_code=ts_code, trade_date=trade_date)

            if df is None or len(df) == 0:
                return 0

            with get_connection() as conn:
                cursor = conn.cursor()
                records = []
                for row in df.itertuples(index=False):
                    row_dict = row._asdict()
                    records.append(
                        (
                            row_dict["ts_code"],
                            row_dict["trade_date"],
                            row_dict.get("buy_sm_amount"),
                            row_dict.get("buy_md_amount"),
                            row_dict.get("buy_lg_amount"),
                            row_dict.get("buy_elg_amount"),
                            row_dict.get("sell_sm_amount"),
                            row_dict.get("sell_md_amount"),
                            row_dict.get("sell_lg_amount"),
                            row_dict.get("sell_elg_amount"),
                            row_dict.get("net_mf"),
                            row_dict.get("pct_mf"),
                        )
                    )

                cursor.executemany(
                    """
                    INSERT OR REPLACE INTO moneyflow
                    (ts_code, trade_date, buy_sm_amount, buy_md_amount,
                     buy_lg_amount, buy_elg_amount, sell_sm_amount,
                     sell_md_amount, sell_lg_amount, sell_elg_amount,
                     net_mf, pct_mf)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    records,
                )

            self._log_sync("moneyflow", ts_code, trade_date, "success")
            return len(df)

        except Exception as e:
            logger.error(f"资金流向同步失败 {ts_code} {trade_date}: {e}")
            self._log_sync("moneyflow", ts_code, "", "failed", str(e))
            return 0

    # ==================== 工具方法 ====================

    def get_sync_status(self) -> dict[str, Any]:
        """获取同步状态"""
        with get_connection() as conn:
            cursor = conn.cursor()

            # 各表数据量
            cursor.execute("SELECT COUNT(*) as cnt FROM stock_basic")
            stock_count = cursor.fetchone()["cnt"]

            cursor.execute("SELECT COUNT(*) as cnt FROM daily_kline")
            kline_count = cursor.fetchone()["cnt"]

            # 最后同步时间
            cursor.execute("""
                SELECT data_type, last_date, status, created_at
                FROM sync_log
                WHERE id IN (
                    SELECT MAX(id) FROM sync_log GROUP BY data_type
                )
            """)
            sync_status = [dict(row) for row in cursor.fetchall()]

            return {
                "stock_count": stock_count,
                "kline_count": kline_count,
                "db_path": str(get_db_path()),
                "sync_status": sync_status,
            }


# ==================== 命令行工具 ====================


def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="工作空间数据同步工具")
    parser.add_argument("--market", choices=["US", "CN"], default="US", help="工作空间市场（默认US）")
    parser.add_argument(
        "action",
        choices=["init", "sync", "status", "stk-factor"],
        help="操作: init=初始化数据库, sync=同步数据, status=查看状态, stk-factor=同步Tushare官方指标",
    )
    parser.add_argument("--ts_code", help="股票代码，如 000001.SZ")
    parser.add_argument("--days", type=int, default=730, help="同步天数")
    parser.add_argument("--indicators", action="store_true", help="同步完成后计算并缓存技术指标（indicator_cache 表）")
    parser.add_argument(
        "--skip-indicators",
        action="store_true",
        help="跳过指标缓存同步（默认单只股票自动同步，批量需指定 --indicators）",
    )

    args = parser.parse_args()

    from .database import market_context

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    with market_context(args.market):
        return run_workspace_sync_command(args.action, ts_code=args.ts_code, days=args.days,
                                          indicators=args.indicators, skip_indicators=args.skip_indicators)


if __name__ == "__main__":
    main()
