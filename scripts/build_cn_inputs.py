#!/usr/bin/env python3
"""生成免费CN名单快照与抽样预检输入；不改业务库，拒绝覆盖既有输出目录。"""

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from modules.cn_initializer import build_cn_input_report  # noqa: E402
from modules.data_sync import download_csindex_closeweight  # noqa: E402
from modules.tushare_client import TushareClient  # noqa: E402
from scripts.init_cn_database import _five_year_start  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", required=True, help="固定已收盘截止日YYYYMMDD")
    parser.add_argument("--start", help="历史抽样起点，默认截止日前五年")
    parser.add_argument("--output", required=True, type=Path, help="新的输出目录，建议data/.cn-init-inputs-日期")
    parser.add_argument("--sample-code", default="600519.SH", help="个股抽样代码；全量仍需初始化逐股验证")
    args = parser.parse_args(argv)
    first = args.start or _five_year_start(args.cutoff)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    result = build_cn_input_report(TushareClient(), download_csindex_closeweight(), first,
                                   args.cutoff, sample_code=args.sample_code)
    for name, value in (("preflight", result["preflight"]), ("snapshot", result["snapshot"])):
        if value is not None:
            with (output / f"{name}.json").open("x", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
    passed = result["preflight"]["passed"]
    print(json.dumps({"passed": passed, "output": str(output), "all_assets_verified": False}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError):
        print("CN输入生成失败：请检查输出目录、免费源可用性及配置；未写入业务数据库。", file=sys.stderr)
        raise SystemExit(1)
