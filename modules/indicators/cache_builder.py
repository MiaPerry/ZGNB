"""指标快照的共享计算：历史序列一次预热，只构造指定日期的行。"""

from . import core
from .price_patterns.brick import precompute_brick_sequence


class IndicatorContext:
    def __init__(self, klines):
        self.klines = klines
        self.kdj = core.precompute_kdj_sequence(klines)
        self.macd = core.precompute_macd_sequence(klines)
        self.bricks = precompute_brick_sequence(klines)
        self.brick_states = []
        color, count = None, 0
        for i, value in enumerate(self.bricks):
            if i >= 8:
                next_color = "RED" if value >= self.bricks[i - 1] else "GREEN"
                count = count + 1 if color == next_color else 1
                color = next_color
            self.brick_states.append((color, count) if i >= 9 else ("NEUTRAL", 0))

    def signal_context(self, i):
        return {
            "kdj": self.kdj[i],
            "kdj_history": self.kdj[:i + 1],
            "macd": (self.macd[0][25:i + 1], self.macd[1][33:i + 1], self.macd[2][33:i + 1]),
        }

    def row(self, i):
        from modules import indicators as ind

        bars = self.klines[:i + 1]
        today = bars[-1]
        yesterday = bars[-2] if i else None
        n = i + 1
        result = {key: getattr(today, key) for key in
                  ("ts_code", "trade_date", "close", "open", "high", "low", "vol", "pct_chg")}

        def put(keys, values):
            result.update(zip(keys.split(), values))

        put("k d j", self.kdj[i])
        put("dif dea macd_hist", [seq[i] for seq in self.macd] if n >= 30 else (0., 0., 0.))
        result["bbi"] = ind.calculate_bbi(bars) if n >= 24 else 0
        closes = [b.close for b in bars]
        for period in (5, 10, 20, 60):
            result[f"ma{period}"] = core.calculate_ma(closes, period)
        put("rsi6 rsi12 rsi24", ind.calculate_rsi_multi(bars) if n >= 25 else (50, 50, 50))
        put("wr5 wr10", ind.calculate_wr_multi(bars) if n >= 10 else (-50, -50))
        put("boll_mid boll_upper boll_lower boll_width boll_position",
            ind.calculate_bollinger(bars) if n >= 20 else (0, 0, 0, 0, 50))
        result["vol_ratio"] = ind.calculate_vol_ratio(bars)
        white = ind.calculate_zg_white(bars) if n >= 115 else 0
        yellow = ind.calculate_dg_yellow(bars) if n >= 115 else 0
        put("zg_white dg_yellow", (white, yellow))
        prev_white = ind.calculate_zg_white(bars[:-1]) if n >= 116 else 0
        prev_yellow = ind.calculate_dg_yellow(bars[:-1]) if n >= 116 else 0
        put("is_gold_cross is_dead_cross", (
            n >= 116 and prev_white <= prev_yellow and white > yellow,
            n >= 116 and prev_white >= prev_yellow and white < yellow,
        ))
        put("rsl_short rsl_long is_needle_20", ind.detect_needle_20(bars) if n >= 22 else (50, 50, False))
        result["brick_value"] = self.bricks[i]
        put("brick_trend brick_count", self.brick_states[i])
        result["brick_trend_up"] = ind.detect_brick_trend(bars) if n >= 115 else False
        result["is_fanbao"] = ind.detect_fanbao(bars) if n >= 4 else False
        pattern = ind.detect_volume_pattern(today, yesterday) if yesterday else {}
        for key in ("is_beidou", "is_suoliang", "is_jiayin_zhenyang", "is_jiayang_zhenyin", "is_fangliang_yinxian"):
            result[key] = int(pattern.get(key, False))
        context = self.signal_context(i)
        score, reason, _ = ind.calculate_sell_score(bars, context=context) if n >= 5 else (3, "数据不足", {})
        put("sell_score sell_reason", (score, reason))
        signal = ind.detect_trade_signal(bars, context=context)
        put("signal signal_desc", (signal.value, signal.value))
        put("prev_high prev_low", (yesterday.high, yesterday.low) if yesterday else (0, 0))
        put("dmi_plus dmi_minus adx", ind.calculate_dmi(bars) if n >= 30 else (0, 0, 0))
        result.update(net_lg_mf=0, net_elg_mf=0, last_b1_date=None, last_b1_price=0,
                      last_yidong_date=None, market_pct_chg=0, market_dir="NEUTRAL", updated_at=None)
        return result


def missing_dates(conn, ts_code):
    return [row[0] for row in conn.execute(
        "SELECT k.trade_date FROM daily_kline k LEFT JOIN indicator_cache i "
        "ON i.ts_code=k.ts_code AND i.trade_date=k.trade_date "
        "WHERE k.ts_code=? AND i.ts_code IS NULL ORDER BY k.trade_date", (ts_code,))]


def write_indicator_rows(ts_code, *, days=None, incremental=True):
    from modules.database import get_connection
    from .data_layer import get_kline_data, clear_indicator_memory_cache

    with get_connection() as conn:
        targets = set(missing_dates(conn, ts_code)) if incremental else None
        if incremental and not targets:
            return 0
    klines = get_kline_data(ts_code, days if days is not None else -1)
    if not klines:
        return 0
    context = IndicatorContext(klines)
    rows = [context.row(i) for i, bar in enumerate(klines) if targets is None or bar.trade_date in targets]
    written = 0
    with get_connection() as conn:
        for row in rows:
            columns = list(row)
            conflict = " ON CONFLICT(ts_code, trade_date) DO NOTHING" if incremental else (
                " ON CONFLICT(ts_code, trade_date) DO UPDATE SET " +
                ",".join(f"{key}=excluded.{key}" for key in columns if key not in ("ts_code", "trade_date")))
            cursor = conn.execute(
                f"INSERT INTO indicator_cache ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})" + conflict,
                tuple(row.values()),
            )
            written += cursor.rowcount
        if incremental and targets.intersection(missing_dates(conn, ts_code)):
            raise RuntimeError(f"指标缺口未补齐: {ts_code}")
        if written:
            from modules.data_freshness import record_data_change
            record_data_change(conn, ts_code, klines[-1].trade_date)
    clear_indicator_memory_cache()
    return written
