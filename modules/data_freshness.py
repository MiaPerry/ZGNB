"""持久化数据版本和就绪状态；读取不写库，版本与行情/指标事务共同提交。"""

from contextlib import closing, contextmanager
import hashlib
import logging
import threading
import uuid

from .database import get_connection, get_db_connection, get_db_path, get_market, market_context

logger = logging.getLogger(__name__)
_HEARTBEAT_SECONDS = 30


def rotate_data_epoch(conn):
    """导入/恢复交付物切换代次，只在目标库事务内调用，不改变源库。"""
    epoch = uuid.uuid4().hex
    conn.execute("INSERT INTO sync_log(data_type,status,message) VALUES ('data_epoch','success',?)", (epoch,))
    return epoch


def record_data_change(conn, ts_code, trade_date):
    """调用方必须在实际写入的同一事务内调用；零写入不调用。"""
    if not conn.execute("SELECT 1 FROM sync_log WHERE data_type='data_epoch' LIMIT 1").fetchone():
        rotate_data_epoch(conn)
    conn.execute(
        "INSERT INTO sync_log(data_type,ts_code,last_date,status,message) VALUES ('data_revision',?,?,'success',?)",
        (ts_code, trade_date, uuid.uuid4().hex),
    )


def _legacy_epoch(path):
    """旧库文件身份只读计算；快照建立时固定，首次写入迁移为持久代次。"""
    stat = path.stat()
    return hashlib.sha256(f"{path}:{stat.st_ino}:{stat.st_ctime_ns}".encode()).hexdigest()[:24]


def _epoch(conn):
    row = conn.execute("SELECT message FROM sync_log WHERE data_type='data_epoch' ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        return row[0]
    frozen = getattr(conn, "fallback_data_epoch", None)
    return frozen if frozen is not None else _legacy_epoch(get_db_path())


def _version(conn, ts_code=None):
    sql = "SELECT message FROM sync_log WHERE data_type='data_revision'"
    args = ()
    if ts_code is not None:
        sql += " AND ts_code=?"
        args = (ts_code,)
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    return f"{_epoch(conn)}:{row[0] if row else '0'}"


def get_data_version(ts_code=None):
    with closing(get_db_connection()) as conn:
        conn.execute("BEGIN")
        return _version(conn, ts_code)


def _cn_basis_status(conn, code):
    """CN就绪要求完整发布依据；辅助规则缺失独立表示未知。"""
    from .data_sync import CN_PRICE_BASIS_VERSION, CN_INDEX_BASIS_VERSION

    asset = conn.execute("SELECT asset_type FROM cn_asset_metadata WHERE ts_code=?", (code,)).fetchone()
    asset_type = asset[0] if asset else None
    rules = ({"status": "not_applicable", "reason": "指数不适用个股涨跌停规则"} if asset_type == "index" else
             {"status": "unknown", "reason": "尚未取得逐日源端涨跌停价格，不能判定规则"})
    details = {"asset_type": asset_type, "rules": rules, "basis_ready": False}
    publication = conn.execute("SELECT * FROM cn_daily_publication WHERE ts_code=?", (code,)).fetchone()
    latest = conn.execute("SELECT MAX(trade_date),COUNT(*) FROM daily_kline WHERE ts_code=?", (code,)).fetchone()
    basis = CN_INDEX_BASIS_VERSION if asset_type == "index" else CN_PRICE_BASIS_VERSION
    if (asset_type is None or not publication or not latest[0] or publication["asset_type"] != asset_type
            or publication["basis_version"] != basis or publication["anchor_date"] != latest[0]):
        return details
    raw_count = conn.execute("SELECT COUNT(*) FROM cn_raw_daily WHERE ts_code=?", (code,)).fetchone()[0]
    factor_count = conn.execute("SELECT COUNT(*) FROM cn_adj_factor WHERE ts_code=?", (code,)).fetchone()[0]
    if raw_count != latest[1] or factor_count != (latest[1] if asset_type == "stock" else 0):
        return details
    anchor = publication["anchor_factor"]
    if asset_type == "stock":
        factor = conn.execute("SELECT adj_factor FROM cn_adj_factor WHERE ts_code=? AND trade_date=?", (code, latest[0])).fetchone()
        if not factor or not anchor or anchor <= 0 or anchor != factor[0]:
            return details
    elif anchor is not None:
        return details
    ratio = "f.adj_factor / ?" if asset_type == "stock" else "1.0"
    conditions = ["r.ts_code IS NULL", "i.ts_code IS NULL", "r.basis_version != ?"]
    args = [basis]
    for key in ("open", "high", "low", "close"):
        conditions.append(f"ABS(k.{key}-r.{key}*({ratio}))>MAX(1e-9,ABS(k.{key})*1e-10)")
        if asset_type == "stock":
            args.append(anchor)
        conditions.append(f"k.{key} IS NULL OR i.{key} IS NULL OR k.{key}!=i.{key}")
    conditions.extend(["k.vol!=r.vol", "k.amount!=r.amount", "k.pct_chg!=r.pct_chg"])
    if asset_type == "stock":
        conditions.append("f.ts_code IS NULL OR f.adj_factor<=0")
    invalid = conn.execute(
        "SELECT COUNT(*) FROM daily_kline k LEFT JOIN cn_raw_daily r USING(ts_code,trade_date) "
        "LEFT JOIN cn_adj_factor f USING(ts_code,trade_date) LEFT JOIN indicator_cache i USING(ts_code,trade_date) "
        f"WHERE k.ts_code=? AND ({' OR '.join(conditions)})", [code, *args],
    ).fetchone()[0]
    details["basis_ready"] = not invalid
    return details


def get_data_status(ts_code=None):
    """集合查询每个标的的真实日期与缺口，不能用整个市场最大日期冒充全部更新。"""
    with closing(get_db_connection()) as conn:
        conn.execute("BEGIN")
        version = _version(conn, ts_code)
        epoch = _epoch(conn)
        revisions = dict(conn.execute(
            "SELECT ts_code,message FROM sync_log WHERE id IN "
            "(SELECT MAX(id) FROM sync_log WHERE data_type='data_revision' GROUP BY ts_code)"))
        rows = conn.execute("""
            WITH codes AS (SELECT ts_code FROM stock_basic UNION SELECT ts_code FROM daily_kline),
            bars AS (SELECT k.ts_code, COUNT(*) AS days, MAX(k.trade_date) AS data_date,
                     MAX(CASE WHEN i.ts_code IS NOT NULL THEN k.trade_date END) AS indicator_date,
                     SUM(CASE WHEN i.ts_code IS NULL THEN 1 ELSE 0 END) AS missing
                     FROM daily_kline k LEFT JOIN indicator_cache i
                     ON i.ts_code=k.ts_code AND i.trade_date=k.trade_date GROUP BY k.ts_code),
            orphans AS (SELECT i.ts_code,COUNT(*) AS n FROM indicator_cache i LEFT JOIN daily_kline k
                       ON k.ts_code=i.ts_code AND k.trade_date=i.trade_date
                       WHERE k.ts_code IS NULL GROUP BY i.ts_code)
            SELECT c.ts_code,b.days,b.data_date,b.indicator_date,b.missing,COALESCE(o.n,0)
            FROM codes c LEFT JOIN bars b ON b.ts_code=c.ts_code LEFT JOIN orphans o ON o.ts_code=c.ts_code
            WHERE (? IS NULL OR c.ts_code=?) ORDER BY c.ts_code
        """, (ts_code, ts_code)).fetchall()
        updating = bool(conn.execute(
            "SELECT 1 FROM sync_log WHERE data_type='data_batch' AND status='running' "
            "AND created_at > datetime('now','-120 seconds') LIMIT 1").fetchone())
        cn_details = {row[0]: _cn_basis_status(conn, row[0]) for row in rows} if get_market() == "CN" else {}
    stocks = {}
    for code, days, date, indicator_date, missing, orphan in rows:
        stocks[code] = {"data_date": date, "indicator_date": indicator_date, "days": days or 0,
                        "missing_indicators": missing or 0, "orphan_indicators": orphan,
                        "ready": bool(days and not missing),
                        "version": f"{epoch}:{revisions.get(code, '0')}"}
        if code in cn_details:
            stocks[code].update(cn_details[code])
            stocks[code]["ready"] = bool(stocks[code]["ready"] and not orphan and cn_details[code]["basis_ready"])
    markets = {}
    for market in (("CN",) if get_market() == "CN" else ("US", "HK")):
        suffixes = (".SH", ".SZ") if market == "CN" else ('.' + market,)
        entries = [s for code, s in stocks.items() if code.endswith(suffixes)]
        dates = [s["data_date"] for s in entries if s["data_date"]]
        markets[market] = {"oldest_date": min(dates, default=None), "latest_date": max(dates, default=None),
                           "not_ready": sum(not s["ready"] for s in entries)}
    return {"workspace_market": get_market(), "version": version, "updating": updating, "stocks": stocks, "markets": markets,
            "ready": bool(stocks) and all(s["ready"] for s in stocks.values())}


@contextmanager
def data_update_batch():
    """心跳仅合并页面刷新，不承担数据版本发布；崩溃后两分钟租约自动失效。"""
    market, path = get_market(), get_db_path()
    with market_context(market, path), get_connection() as conn:
        batch_id = conn.execute(
            "INSERT INTO sync_log(data_type,status,message) VALUES ('data_batch','running',?)",
            (uuid.uuid4().hex,),
        ).lastrowid
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(_HEARTBEAT_SECONDS):
            try:
                with market_context(market, path), get_connection() as conn:
                    conn.execute("UPDATE sync_log SET created_at=CURRENT_TIMESTAMP WHERE id=?", (batch_id,))
            except Exception:
                logger.warning("更新批次心跳失败", exc_info=True)

    thread = threading.Thread(target=heartbeat, daemon=True, name="data-update-heartbeat")
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()
        with market_context(market, path), get_connection() as conn:
            conn.execute("UPDATE sync_log SET status='finished', created_at=CURRENT_TIMESTAMP WHERE id=?", (batch_id,))
