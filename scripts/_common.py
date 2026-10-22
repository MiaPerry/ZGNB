"""
scripts/ 共享工具模块
v2.10.0 提取：消除 4 个薄壳脚本中重复的 _load_watchlist()
"""

import json
import logging
import os
from pathlib import Path

from modules.database import get_market, get_watchlist, validate_market_code

logger = logging.getLogger(__name__)

# 项目根目录（scripts/ 的父目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_watchlist() -> list[str]:
    """
    读取当前库自选；显式 STOCKS_JSON 可覆盖，代码校验归属并去重。

    JSON 格式：[{code, name}, ...]；仅 CN 兼容六位数字旧名单。
    """
    path = os.environ.get("STOCKS_JSON")
    if not path:
        return list(dict.fromkeys(validate_market_code(item["ts_code"]) for item in get_watchlist()))
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
    except FileNotFoundError:
        logger.error(f"自选股文件不存在: {path}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"自选股文件格式错误: {path} — {e}")
        return []

    result = []
    for item in items:
        code = item["code"]
        if get_market() == "CN" and isinstance(code, str) and len(code) == 6 and code.isdigit():
            code += ".SH" if code.startswith("6") else ".SZ"
        result.append(validate_market_code(code))
    return list(dict.fromkeys(result))
