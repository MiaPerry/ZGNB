#!/usr/bin/env python3
"""
美股数据同步脚本

使用 yfinance（雅虎财经免费 API）拉取美股 K 线数据，
写入项目现有的 daily_kline / indicator_cache 表，与 A 股共用同一套分析引擎。

依赖：pip install yfinance

用法：
    python sync_us_stocks.py                    # 同步默认美股龙头
    python sync_us_stocks.py --tickers AAPL,MSFT,NVDA  # 同步指定股票
    python sync_us_stocks.py --days 180         # 指定同步天数
"""

import os
import sys
import time
import sqlite3
import datetime
import argparse

from dotenv import load_dotenv
load_dotenv(override=True)

# ── 美股龙头股列表 ──
# (ticker, name, sector)
US_SECTOR_LEADERS = [
    # 科技巨头
    ("AAPL", "苹果", "消费电子"),
    ("MSFT", "微软", "软件服务"),
    ("GOOGL", "谷歌", "互联网"),
    ("AMZN", "亚马逊", "电商云"),
    ("META", "Meta", "社交平台"),
    ("NVDA", "英伟达", "AI芯片"),
    ("TSLA", "特斯拉", "新能源汽车"),
    ("AVGO", "博通", "半导体"),
    ("ORCL", "甲骨文", "企业软件"),
    ("ADBE", "Adobe", "创意软件"),
    ("CRM", "Salesforce", "企业云"),
    ("AMD", "AMD", "半导体"),
    ("INTC", "英特尔", "半导体"),
    ("QCOM", "高通", "通信芯片"),
    ("CSCO", "思科", "网络设备"),

    # AI / 云计算
    ("PLTR", "Palantir", "数据分析AI"),
    ("CRWD", "CrowdStrike", "网络安全"),
    ("SNOW", "Snowflake", "数据云"),
    ("DDOG", "Datadog", "云监控"),
    ("MDB", "MongoDB", "数据库"),

    # 金融
    ("JPM", "摩根大通", "银行"),
    ("BAC", "美国银行", "银行"),
    ("WFC", "富国银行", "银行"),
    ("GS", "高盛", "投行"),
    ("MS", "摩根士丹利", "投行"),
    ("V", "Visa", "支付"),
    ("MA", "Mastercard", "支付"),
    ("BRK-B", "伯克希尔", "保险控股"),

    # 消费 / 医疗
    ("WMT", "沃尔玛", "零售"),
    ("COST", "好市多", "零售"),
    ("HD", "家得宝", "家居零售"),
    ("MCD", "麦当劳", "餐饮"),
    ("NKE", "耐克", "运动品牌"),
    ("DIS", "迪士尼", "传媒娱乐"),
    ("NFLX", "奈飞", "流媒体"),
    ("JNJ", "强生", "医疗健康"),
    ("UNH", "联合健康", "医疗保险"),
    ("PFE", "辉瑞", "制药"),
    ("LLY", "礼来", "制药"),

    # 能源 / 工业
    ("XOM", "埃克森美孚", "石油"),
    ("CVX", "雪佛龙", "石油"),
    ("BA", "波音", "航空航天"),
    ("CAT", "卡特彼勒", "工程机械"),
    ("GE", "通用电气", "工业集团"),
    ("LMT", "洛克希德马丁", "军工"),
]


def fetch_us_kline(ticker: str, days: int):
    """用 yfinance 拉取美股 K 线数据"""
    import yfinance as yf

    end = datetime.date.today()
    start = end - datetime.timedelta(days=days * 2)  # 多拉一些覆盖休市日

    df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)

    if df is None or len(df) == 0:
        return []

    records = []
    for date, row in df.iterrows():
        try:
            open_p = float(row["Open"])
            high = float(row["High"])
            low = float(row["Low"])
            close = float(row["Close"])
            vol = float(row["Volume"])
            # yfinance 返回的 adj close 用于复权参考
            adj_close = float(row.get("Adj Close", close))

            # 计算涨跌幅
            if records:
                prev_close = records[-1]["close"]
                pct_chg = round((close / prev_close - 1) * 100, 4) if prev_close > 0 else 0.0
            else:
                pct_chg = 0.0

            trade_date = date.strftime("%Y%m%d")
            records.append({
                "ts_code": f"{ticker}.US",
                "trade_date": trade_date,
                "open": open_p,
                "high": high,
                "low": low,
                "close": close,
                "vol": vol,
                "amount": round(close * vol, 2),
                "pct_chg": pct_chg,
            })
        except (ValueError, TypeError, KeyError):
            continue

    return records


def insert_stock_basic(conn, ticker: str, name: str, sector: str):
    """写入美股基本信息到 stock_basic 表"""
    ts_code = f"{ticker}.US"
    conn.execute(
        """INSERT OR IGNORE INTO stock_basic
        (ts_code, name, area, industry, market, list_date, is_hs)
        VALUES (?,?,?,?,?,?,?)""",
        (ts_code, name, "美国", sector, "美股", "20000101", "N"),
    )
    conn.commit()


def insert_kline_data(conn, records: list[dict]) -> int:
    """写入 K 线数据到 daily_kline 表"""
    inserted = 0
    for r in records:
        try:
            conn.execute(
                """INSERT OR REPLACE INTO daily_kline
                (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    r["ts_code"],
                    r["trade_date"],
                    r["open"],
                    r["high"],
                    r["low"],
                    r["close"],
                    r["vol"],
                    r["amount"],
                    r["pct_chg"],
                ),
            )
            inserted += 1
        except Exception:
            pass
    conn.commit()
    return inserted


def compute_indicators(ts_code: str):
    """计算技术指标缓存"""
    try:
        from modules.data_sync import DataSyncer
        syncer = DataSyncer()
        cnt = syncer.sync_indicator_cache(ts_code)
        return cnt
    except Exception as e:
        print(f"    指标计算失败: {e}")
        return 0


def main():
    parser = argparse.ArgumentParser(description="美股数据同步")
    parser.add_argument("--tickers", type=str, default="", help="指定股票代码，逗号分隔（如 AAPL,MSFT,NVDA）")
    parser.add_argument("--days", type=int, default=120, help="同步天数（默认120）")
    args = parser.parse_args()

    db_path = os.getenv("DB_PATH", "data/stock_data.db")
    conn = sqlite3.connect(db_path)

    # 确定股票列表
    if args.tickers:
        stocks = [(t.strip().upper(), t.strip().upper(), "自定义") for t in args.tickers.split(",")]
    else:
        stocks = US_SECTOR_LEADERS

    print("=" * 60)
    print(f"美股数据同步 — {len(stocks)} 只股票，最近 {args.days} 天")
    print("=" * 60)

    # Step 1: 写入基本信息
    print("\nStep 1: 写入美股基本信息...")
    for ticker, name, sector in stocks:
        insert_stock_basic(conn, ticker, name, sector)
    c = conn.execute("SELECT COUNT(*) FROM stock_basic WHERE ts_code LIKE '%.US'")
    print(f"  stock_basic 表中美股: {c.fetchone()[0]} 只")

    # Step 2: 拉取 K 线数据
    print(f"\nStep 2: 用 yfinance 拉取 K 线数据...")
    total_records = 0
    success = 0
    fail = 0

    for i, (ticker, name, sector) in enumerate(stocks):
        ts_code = f"{ticker}.US"
        try:
            records = fetch_us_kline(ticker, args.days)
            if not records:
                print(f"  [{i+1}/{len(stocks)}] {ts_code} {name} — 无数据")
                fail += 1
                continue

            inserted = insert_kline_data(conn, records)
            total_records += inserted
            success += 1
            print(f"  [{i+1}/{len(stocks)}] {ts_code} {name} — {inserted} 条K线")

            time.sleep(0.3)  # 避免请求过快

        except Exception as e:
            print(f"  [{i+1}/{len(stocks)}] {ts_code} {name} — 失败: {e}")
            fail += 1

    print(f"\nK线同步完成: 成功 {success}, 失败 {fail}, 总记录 {total_records} 条")

    # Step 3: 计算指标缓存
    print(f"\nStep 3: 计算技术指标缓存...")
    c = conn.execute("SELECT DISTINCT ts_code FROM daily_kline WHERE ts_code LIKE '%.US'")
    us_codes = [row[0] for row in c.fetchall()]

    ind_success = 0
    for i, ts_code in enumerate(us_codes):
        cnt = compute_indicators(ts_code)
        if cnt > 0:
            ind_success += 1
        print(f"  [{i+1}/{len(us_codes)}] {ts_code} — {cnt} 条指标")

    print(f"\n指标计算完成: {ind_success}/{len(us_codes)}")

    # 汇总
    print("\n" + "=" * 60)
    print("同步完成汇总")
    print("=" * 60)

    c = conn.execute("SELECT COUNT(*) FROM stock_basic WHERE ts_code LIKE '%.US'")
    print(f"  美股基本信息: {c.fetchone()[0]} 只")

    c = conn.execute("SELECT COUNT(*) FROM daily_kline WHERE ts_code LIKE '%.US'")
    print(f"  美股K线数据: {c.fetchone()[0]} 条")

    c = conn.execute("SELECT COUNT(*) FROM indicator_cache WHERE ts_code LIKE '%.US'")
    print(f"  美股指标缓存: {c.fetchone()[0]} 条")

    conn.close()
    print("\n提示: 前端输入美股代码（如 AAPL）即可查看分析结果")


if __name__ == "__main__":
    main()
