#!/usr/bin/env python3
"""
A股各板块龙头股 - 批量同步脚本
1. 写入板块龙头股基本信息到 stock_basic 表
2. 用 pro.daily() 拉取K线数据
3. 计算技术指标缓存
"""

import os
import sys
import time
import sqlite3
import datetime

# 先加载环境变量
from dotenv import load_dotenv
load_dotenv(override=True)

import tushare as ts

# ── 配置 ──
DB_PATH = os.getenv("DB_PATH", "data/stock_data.db")
TOKEN = os.getenv("TUSHARE_TOKEN", "")
API_URL = os.getenv("TUSHARE_API_URL", "")
SYNC_DAYS = 120  # 同步最近120个交易日

# ── A股各板块龙头股 ──
# (ts_code, name, area, industry, market, list_date, is_hs)
SECTOR_LEADERS = [
    # ═══ AI算力/科技成长 ═══
    ("601138.SH", "工业富联", "广东", "电子制造", "主板", "20180608", "S"),
    ("300308.SZ", "中际旭创", "江苏", "通信设备", "创业板", "20170110", "S"),
    ("300502.SZ", "新易盛", "四川", "通信设备", "创业板", "20160720", "S"),
    ("300394.SZ", "天孚通信", "江苏", "通信设备", "创业板", "20151202", "S"),
    ("603019.SH", "中科曙光", "天津", "计算机设备", "主板", "20141106", "S"),
    ("000063.SZ", "中兴通讯", "深圳", "通信设备", "主板", "19971118", "S"),
    ("688256.SH", "寒武纪", "北京", "半导体", "科创板", "20200720", "S"),
    ("688041.SH", "海光信息", "天津", "半导体", "科创板", "20220812", "S"),
    ("002371.SZ", "北方华创", "北京", "半导体设备", "主板", "20100316", "S"),
    ("688981.SH", "中芯国际", "上海", "半导体", "科创板", "20200716", "S"),
    ("603986.SH", "兆易创新", "北京", "半导体", "主板", "20160818", "S"),
    ("688111.SH", "金山办公", "北京", "软件服务", "科创板", "20191118", "S"),

    # ═══ AI应用/软件 ═══
    ("002230.SZ", "科大讯飞", "安徽", "软件服务", "主板", "20080512", "S"),
    ("300033.SZ", "同花顺", "浙江", "软件服务", "创业板", "20091225", "S"),
    ("300418.SZ", "昆仑万维", "北京", "互联网服务", "创业板", "20150121", "S"),

    # ═══ 新能源/能源转型 ═══
    ("300750.SZ", "宁德时代", "福建", "电池", "创业板", "20180611", "S"),
    ("002594.SZ", "比亚迪", "广东", "汽车整车", "主板", "20110630", "S"),
    ("300274.SZ", "阳光电源", "安徽", "光伏设备", "创业板", "20110525", "S"),
    ("601012.SH", "隆基绿能", "陕西", "光伏设备", "主板", "20120511", "S"),
    ("600438.SH", "通威股份", "四川", "光伏设备", "主板", "20040302", "S"),
    ("300014.SZ", "亿纬锂能", "广东", "电池", "创业板", "20091030", "S"),

    # ═══ 机器人/智能制造 ═══
    ("300124.SZ", "汇川技术", "深圳", "自动化设备", "创业板", "20100928", "S"),
    ("002050.SZ", "三花智控", "浙江", "汽车零部件", "主板", "20051224", "S"),
    ("002747.SZ", "埃斯顿", "江苏", "机器人", "主板", "20150320", "S"),
    ("688017.SH", "绿的谐波", "江苏", "机器人", "科创板", "20200828", "S"),

    # ═══ 消费核心资产 ═══
    ("600519.SH", "贵州茅台", "贵州", "白酒", "主板", "20010827", "S"),
    ("000858.SZ", "五粮液", "四川", "白酒", "主板", "19980427", "S"),
    ("000333.SZ", "美的集团", "广东", "家电", "主板", "20130918", "S"),
    ("600887.SH", "伊利股份", "内蒙古", "乳品", "主板", "19960312", "S"),
    ("603288.SH", "海天味业", "广东", "调味品", "主板", "20140211", "S"),
    ("601888.SH", "中国中免", "北京", "免税零售", "主板", "20091015", "S"),

    # ═══ 医疗健康 ═══
    ("300760.SZ", "迈瑞医疗", "深圳", "医疗器械", "创业板", "20181016", "S"),
    ("600276.SH", "恒瑞医药", "江苏", "化学制药", "主板", "20001018", "S"),
    ("603259.SH", "药明康德", "江苏", "医疗服务", "主板", "20180508", "S"),

    # ═══ 金融资产 ═══
    ("600036.SH", "招商银行", "深圳", "银行", "主板", "20020409", "S"),
    ("600030.SH", "中信证券", "深圳", "证券", "主板", "20030106", "S"),
    ("601318.SH", "中国平安", "深圳", "保险", "主板", "20070301", "S"),
    ("300059.SZ", "东方财富", "上海", "证券", "创业板", "20100319", "S"),

    # ═══ 资源周期 ═══
    ("601899.SH", "紫金矿业", "福建", "有色金属", "主板", "20080425", "S"),
    ("601088.SH", "中国神华", "北京", "煤炭开采", "主板", "20071009", "S"),
    ("603993.SH", "洛阳钼业", "河南", "有色金属", "主板", "20071009", "S"),
    ("600309.SH", "万华化学", "山东", "化工", "主板", "20010105", "S"),

    # ═══ 电力/公用事业 ═══
    ("600900.SH", "长江电力", "北京", "水电", "主板", "20031028", "S"),
    ("600406.SH", "国电南瑞", "江苏", "电网设备", "主板", "20031016", "S"),

    # ═══ 军工制造 ═══
    ("600760.SH", "中航沈飞", "辽宁", "航空装备", "主板", "19960408", "S"),
    ("600893.SH", "航发动力", "北京", "航空发动机", "主板", "19980318", "S"),

    # ═══ 基建制造 ═══
    ("601668.SH", "中国建筑", "北京", "房屋建设", "主板", "20090729", "S"),
    ("600660.SH", "福耀玻璃", "福建", "汽车零部件", "主板", "19931206", "S"),
]


def main():
    # ── 初始化 Tushare ──
    pro = ts.pro_api(TOKEN)
    if API_URL:
        pro._DataApi__http_url = API_URL

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # ═══ Step 1: 写入股票基本信息 ═══
    print("=" * 60)
    print(f"Step 1: 写入 {len(SECTOR_LEADERS)} 只板块龙头股基本信息")
    print("=" * 60)

    inserted = 0
    skipped = 0
    for stock in SECTOR_LEADERS:
        ts_code = stock[0]
        c.execute("SELECT 1 FROM stock_basic WHERE ts_code=?", (ts_code,))
        if c.fetchone():
            skipped += 1
            continue
        c.execute(
            "INSERT OR IGNORE INTO stock_basic (ts_code, name, area, industry, market, list_date, is_hs) VALUES (?,?,?,?,?,?,?)",
            stock
        )
        inserted += 1

    conn.commit()
    c.execute("SELECT COUNT(*) FROM stock_basic")
    total = c.fetchone()[0]
    print(f"  新增: {inserted} 只, 跳过(已存在): {skipped} 只, 总计: {total} 只")

    # ═══ Step 2: 拉取K线数据 ═══
    print()
    print("=" * 60)
    print(f"Step 2: 用 pro.daily() 拉取最近 {SYNC_DAYS} 天K线数据")
    print("=" * 60)

    end_date = datetime.date.today().strftime("%Y%m%d")
    start_date = (datetime.date.today() - datetime.timedelta(days=SYNC_DAYS * 2)).strftime("%Y%m%d")

    c.execute("SELECT ts_code FROM stock_basic")
    all_codes = [row[0] for row in c.fetchall()]

    success_count = 0
    fail_count = 0
    total_records = 0

    for i, ts_code in enumerate(all_codes):
        # 检查是否已有足够数据
        c.execute("SELECT COUNT(*) FROM daily_kline WHERE ts_code=?", (ts_code,))
        existing = c.fetchone()[0]
        if existing >= 60:
            print(f"  [{i+1}/{len(all_codes)}] {ts_code} 已有 {existing} 条，跳过")
            success_count += 1
            continue

        try:
            df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)

            if df is None or len(df) == 0:
                print(f"  [{i+1}/{len(all_codes)}] {ts_code} 无数据")
                fail_count += 1
                continue

            # 插入K线数据
            inserted_rows = 0
            for _, row in df.iterrows():
                try:
                    c.execute(
                        """INSERT OR REPLACE INTO daily_kline
                        (ts_code, trade_date, open, high, low, close,
                         pct_chg, vol, amount)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            row.get("ts_code", ts_code),
                            str(row.get("trade_date", "")),
                            float(row.get("open", 0)),
                            float(row.get("high", 0)),
                            float(row.get("low", 0)),
                            float(row.get("close", 0)),
                            float(row.get("pct_chg", 0)),
                            float(row.get("vol", 0)),
                            float(row.get("amount", 0)),
                        )
                    )
                    inserted_rows += 1
                except Exception as ex:
                    print(f"    插入失败: {ex}")
                    pass

            conn.commit()
            total_records += inserted_rows
            success_count += 1
            print(f"  [{i+1}/{len(all_codes)}] {ts_code} 写入 {inserted_rows} 条K线")

            # 每5只暂停一下避免限流
            if (i + 1) % 5 == 0:
                time.sleep(0.5)

        except Exception as e:
            print(f"  [{i+1}/{len(all_codes)}] {ts_code} 失败: {e}")
            fail_count += 1
            # 如果遇到限流，等待较长时间
            if "频率" in str(e) or "超限" in str(e):
                print("  遇到限流，等待60秒...")
                time.sleep(60)

    print(f"\nK线同步完成: 成功 {success_count} 只, 失败 {fail_count} 只, 总记录 {total_records} 条")

    # ═══ Step 3: 计算指标缓存 ═══
    print()
    print("=" * 60)
    print("Step 3: 计算技术指标缓存 (indicator_cache)")
    print("=" * 60)

    try:
        from modules.data_sync import DataSyncer
        syncer = DataSyncer()

        c.execute("SELECT DISTINCT ts_code FROM daily_kline")
        codes_with_kline = [row[0] for row in c.fetchall()]

        ind_success = 0
        for i, ts_code in enumerate(codes_with_kline):
            try:
                cnt = syncer.sync_indicator_cache(ts_code)
                ind_success += 1
                print(f"  [{i+1}/{len(codes_with_kline)}] {ts_code} 指标计算完成 ({cnt} 条)")
            except Exception as e:
                print(f"  [{i+1}/{len(codes_with_kline)}] {ts_code} 指标计算失败: {e}")

        print(f"\n指标计算完成: {ind_success}/{len(codes_with_kline)}")
    except Exception as e:
        print(f"  指标缓存计算失败: {e}")

    # ═══ 汇总 ═══
    print()
    print("=" * 60)
    print("同步完成汇总")
    print("=" * 60)

    c.execute("SELECT COUNT(*) FROM stock_basic")
    print(f"  stock_basic: {c.fetchone()[0]} 只股票")

    c.execute("SELECT COUNT(*) FROM daily_kline")
    print(f"  daily_kline: {c.fetchone()[0]} 条K线记录")

    c.execute("SELECT COUNT(*) FROM indicator_cache")
    print(f"  indicator_cache: {c.fetchone()[0]} 条指标记录")

    c.execute("SELECT ts_code, COUNT(*) as cnt FROM daily_kline GROUP BY ts_code ORDER BY cnt DESC")
    print("\n  各股票K线数据量:")
    for row in c.fetchall():
        print(f"    {row[0]}: {row[1]} 条")

    conn.close()


if __name__ == "__main__":
    main()
