#!/usr/bin/env python3
"""免费CN适配：保留Tushare原始日线，公开源提供因子、日历及指数。"""

from bisect import bisect_right
import json
import math
import re
import threading
import time
from urllib.parse import urlsplit

import pandas as pd
import requests

from .database import validate_market_code


class CNFreeSourceError(ValueError):
    """公共接口错误不包含请求参数、配置凭据或远端响应正文。"""

    def __init__(self, kind):
        self.kind = kind
        reason = {"network": "网络不可用", "rate_limit": "调用限频", "invalid_response": "返回结构无效"}
        super().__init__(f"CN免费源失败：{reason.get(kind, '数据源不可用')}")


_PUBLIC_LOCK = threading.Lock()
_LAST_REQUEST = 0.0
_MAX_BYTES = 2 * 1024 * 1024
_PUBLIC_HOSTS = {"finance.sina.com.cn", "query.sse.com.cn", "www.szse.cn", "datacenter-web.eastmoney.com"}


def public_bytes(url, *, params=None, headers=None):
    """固定公开域名、节流、超时和体积上限；不执行或记录远端正文。"""
    global _LAST_REQUEST
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _PUBLIC_HOSTS or parsed.username or parsed.password:
        raise CNFreeSourceError("invalid_response")
    request_headers = {"User-Agent": "Mozilla/5.0", **(headers or {})}
    with _PUBLIC_LOCK:
        wait = max(0, 0.8 - (time.monotonic() - _LAST_REQUEST))
        if wait:
            time.sleep(wait)
        _LAST_REQUEST = time.monotonic()
        try:
            with requests.get(url, params=params, headers=request_headers, timeout=30, stream=True) as response:
                if response.status_code == 429:
                    raise CNFreeSourceError("rate_limit")
                if response.status_code != 200:
                    raise CNFreeSourceError("network")
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > _MAX_BYTES:
                        raise CNFreeSourceError("invalid_response")
                    chunks.append(chunk)
                if not size:
                    raise CNFreeSourceError("invalid_response")
                return b"".join(chunks)
        except requests.RequestException:
            raise CNFreeSourceError("network") from None


def public_text(url, **kwargs):
    try:
        return public_bytes(url, **kwargs).decode("utf-8-sig")
    except UnicodeError:
        raise CNFreeSourceError("invalid_response") from None


def _day(value):
    text = str(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T00:00:00\.000Z)?", text):
        raise ValueError("新浪日期结构无效")
    return pd.Timestamp(text[:10]).strftime("%Y%m%d")


def _requested_dates(first, last):
    if not all(isinstance(date, str) and re.fullmatch(r"\d{8}", date) for date in (first, last)):
        raise ValueError("CN请求日期无效")
    days = pd.date_range(pd.to_datetime(first, format="%Y%m%d"), pd.to_datetime(last, format="%Y%m%d"))
    if days.empty:
        raise ValueError("CN请求起止日期无效")
    return days.strftime("%Y%m%d").tolist()


def parse_sina_factors(text, symbol):
    """因子响应只解析严格JSON，不使用AKShare的远端eval路径。"""
    if not re.fullmatch(r"(?:sh|sz)\d{6}", symbol):
        raise ValueError("新浪代码无效")
    match = re.fullmatch(r"\s*var\s+" + re.escape(symbol) + r"hfq\s*=\s*(\{.*\})\s*;?\s*", text, re.DOTALL)
    if not match:
        raise ValueError("新浪因子代码或JSON封装无效")
    try:
        payload = json.loads(match[1])
        rows = payload["data"]
        if not isinstance(rows, list) or not rows or payload["total"] != len(rows):
            raise ValueError("新浪因子截断或为空")
        result = {}
        for row in rows:
            date, factor = _day(row["d"]), float(row["f"])
            if date in result or not math.isfinite(factor) or factor <= 0:
                raise ValueError("新浪因子重复或非有限正数")
            result[date] = factor
        return dict(sorted(result.items()))
    except (KeyError, TypeError, json.JSONDecodeError, OverflowError):
        raise ValueError("新浪因子结构无效") from None


def expand_sina_factors(sparse, code, first, last):
    """稀疏因子只在明确生效后沿用；不能把未来因子向前填充。"""
    validate_market_code(code, "CN")
    dates = _requested_dates(first, last)
    effective = sorted(sparse)
    if not effective or dates[0] < effective[0]:
        raise ValueError("请求起点没有已确认生效的复权因子")
    rows = [{"ts_code": code, "trade_date": date,
             "adj_factor": sparse[effective[bisect_right(effective, date) - 1]]} for date in dates]
    return pd.DataFrame(rows)


def decode_sina_series(text):
    """仅把压缩字符串交给固定解码函数；远端JS封装不进入解释器。"""
    # 官网日历附带固定变量别名；只剥离这一个已核验的纯数据赋值。
    if text.strip().startswith("var datelist="):
        text = text.strip().removesuffix("var KLC_TD_SH=datelist;")
    match = re.fullmatch(r'\s*var\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*"([A-Za-z0-9+/=]+)"\s*;?\s*', text)
    if not match:
        raise ValueError("新浪压缩数据封装无效")
    from akshare.stock.cons import hk_js_decode
    from py_mini_racer import MiniRacer

    try:
        with MiniRacer() as runtime:
            runtime.set_hard_memory_limit(64 * 1024 * 1024)
            runtime.eval(hk_js_decode, timeout_sec=5)
            decoded = runtime.call("d", match[1], timeout_sec=5)
        if not isinstance(decoded, list) or not decoded or len(decoded) > 20000:
            raise ValueError("新浪解码返回结构无效")
        return decoded
    except Exception:
        raise ValueError("新浪压缩数据无法安全解码") from None


def build_sina_calendar(decoded, first, last):
    dates = [_day(date) for date in decoded]
    requested = _requested_dates(first, last)
    if not dates or dates != sorted(set(dates)) or requested[0] < dates[0] or requested[-1] > dates[-1]:
        raise ValueError("新浪日历重复、乱序或未覆盖请求范围")
    opened = set(dates)
    return pd.DataFrame({"cal_date": requested, "is_open": [int(date in opened) for date in requested]})


def build_sina_index_daily(decoded, code, first, last):
    """新浪指数股/元转为现有入口的手/千元；候选入口统一转回股/元。"""
    if code != "000300.SH":
        raise ValueError("免费指数接口只接受明确的沪深300指数")
    requested = set(_requested_dates(first, last))
    rows, previous, dates = [], None, []
    try:
        for source in decoded:
            date = _day(source["date"])
            dates.append(date)
            close = float(source["close"])
            reference = float(source.get("prevclose", previous if previous is not None else close))
            if reference <= 0 or not math.isfinite(reference):
                raise ValueError("新浪指数昨收无效")
            if date in requested:
                rows.append({"ts_code": code, "trade_date": date,
                             **{key: float(source[key]) for key in ("open", "high", "low", "close")},
                             "vol": float(source["volume"]) / 100,
                             "amount": float(source["amount"]) / 1000,
                             "pct_chg": (close / reference - 1) * 100})
            previous = close
        if not dates or dates != sorted(set(dates)):
            raise ValueError("新浪指数日期重复或乱序")
        return pd.DataFrame(rows, columns=["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"])
    except (KeyError, TypeError, OverflowError):
        raise ValueError("新浪指数结构无效") from None


class FreeCNClient:
    """单任务缓存公共日历、稀疏因子和指数；股票日线保持原始入口。"""

    def __init__(self, *, raw_client=None, fetch_text=None):
        self._raw_client = raw_client
        self._fetch_text = fetch_text or public_text
        self._calendar = None
        self._factors = {}
        self._index = None
        self._raw_frames = {}

    def get_raw_daily(self, code, first, last):
        validate_market_code(code, "CN")
        if code == "000300.SH":
            raise ValueError("指数不能走个股日线")
        key = (code, first, last)
        if key not in self._raw_frames:
            if self._raw_client is None:
                from .tushare_client import TushareClient
                self._raw_client = TushareClient()
            self._raw_frames[key] = self._raw_client.get_raw_daily(code, first, last).copy()
        return self._raw_frames[key].copy()

    def get_adj_factor(self, code, first, last):
        validate_market_code(code, "CN")
        if code == "000300.SH":
            raise ValueError("指数不得请求复权因子")
        if code not in self._factors:
            number, exchange = code.split(".")
            symbol = exchange.lower() + number
            text = self._fetch_text(f"https://finance.sina.com.cn/realstock/company/{symbol}/hfq.js")
            self._factors[code] = parse_sina_factors(text, symbol)
        return expand_sina_factors(self._factors[code], code, first, last)

    def get_trade_cal(self, *, start_date, end_date, exchange="SSE", raise_on_error=True):
        if exchange not in {"SSE", "SZSE"}:
            raise ValueError("免费CN日历交易所无效")
        if self._calendar is None:
            self._calendar = decode_sina_series(self._fetch_text("https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"))
        return build_sina_calendar(self._calendar, start_date, end_date)

    def get_index_daily(self, code, first, last, *, raise_on_error=True):
        if code != "000300.SH":
            raise ValueError("免费指数接口只接受沪深300")
        if self._index is None:
            self._index = decode_sina_series(self._fetch_text(
                "https://finance.sina.com.cn/realstock/company/sh000300/hisdata_klc2/klc_kl.js"))
        return build_sina_index_daily(self._index, code, first, last)


if __name__ == "__main__":
    print("免费CN源仅通过预检、初始化和市场同步入口显式调用。")
