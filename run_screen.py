#!/usr/bin/env python3
"""多策略选股扫描"""
import requests, json

strategies = [
    ("b1", "B1 左侧抄底"),
    ("perfect", "完美图形"),
    ("super_b1", "超级B1"),
    ("changan", "长安战法"),
    ("build_wave", "建仓波"),
    ("xishou", "吸筹"),
    ("safe", "安全标的"),
    ("oversold", "超卖反弹"),
    ("breakout", "突破"),
]

for criteria, label in strategies:
    try:
        resp = requests.post(
            "http://localhost:8000/api/v1/screen/run",
            json={"strategy": criteria, "limit": 20, "use_parallel": False},
            timeout=120
        )
        data = resp.json()
        total = data.get("total", 0)
        results = data.get("results", [])

        print(f"\n{'='*60}")
        print(f"策略: {label} ({criteria}) — 命中 {total} 只")
        print(f"{'='*60}")

        if results:
            print(f"{'代码':<14} {'名称':<10} {'评分':>6} {'信号':>6} {'行业':<12}")
            print("-" * 60)
            for r in results:
                ts_code = r.get("ts_code", "")
                name = r.get("name", "")
                score = r.get("score", 0)
                signal = r.get("signal", "")
                industry = r.get("industry", "")
                print(f"{ts_code:<14} {name:<10} {score:>6.1f} {signal:>6} {industry:<12}")
        else:
            print("  （无命中）")

    except Exception as e:
        print(f"\n{label}: 请求失败 - {e}")

print("\n" + "=" * 60)
print("扫描完成")
