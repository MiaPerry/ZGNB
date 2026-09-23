#!/usr/bin/env python3
"""只读检查 Yahoo Chart 有效日线；不连接数据库、不入库、不输出代理凭据。"""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import yahoo_sync as yahoo  # noqa: E402


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=30)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    try:
        with closing(yahoo._default_session()) as session:
            payload = yahoo.fetch_chart("AAPL", start, end, session)
        bars = yahoo.parse_chart_bars(payload, "AAPL.US")
        valid = [
            bar
            for bar in bars
            if start <= bar["trade_date"] <= end
            and all(
                isinstance(bar[key], (int, float)) and math.isfinite(bar[key]) and bar[key] > 0
                for key in ("open", "high", "low", "close")
            )
        ]
        if not valid:
            print("NO_DATA：请求已返回，但最近 30 天没有有效 AAPL K 线。", file=sys.stderr)
            return 2
        dates = sorted({bar["trade_date"] for bar in valid})
        print(json.dumps({"symbol": "AAPL", "count": len(valid), "first_date": dates[0], "last_date": dates[-1]}))
        return 0
    except Exception:
        print("CHECK_FAILED：Yahoo 请求或解析失败；请检查服务器自身的 YAHOO_PROXY 与代理服务。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
