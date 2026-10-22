"""
Tushare 中转 API 客户端
支持 Tushare SDK 方式调用中转 API

中转服务文档: http://tsy.xiaodefa.cn/docs.html
"""

import os
import time
import logging
from typing import Optional

try:
    import requests  # noqa: F401  可用性检查
    import pandas as pd
    import tushare as ts
except ImportError:
    print("请先安装依赖: pip install requests pandas python-dotenv tushare")

# dotenv 加载已移至 modules/__init__.py（包级别一次性加载，override=True）

logger = logging.getLogger(__name__)

TUSHARE_API_URL = os.environ.get("TUSHARE_API_URL", "")
VERIFY_TOKEN_URL = os.environ.get("TUSHARE_VERIFY_TOKEN_URL", "")
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")


class TushareDataError(RuntimeError):
    """CN接口失败的安全契约；不把可能含凭据的源端错误写入日志或任务。"""

    def __init__(self, endpoint: str, kind: str):
        self.endpoint = endpoint
        self.kind = kind
        reasons = {"auth": "权限或配置不足", "rate_limit": "调用限频", "network": "网络不可用",
                   "invalid_response": "返回结构无效", "config": "未启用已配置的Tushare服务"}
        super().__init__(f"Tushare {endpoint} 失败：{reasons.get(kind, '数据源异常')}")


class TushareClient:
    """Tushare 中转 SDK 客户端

    使用官方 Tushare SDK 配置中转 URL，支持：
    - get_daily: 个股日线
    - get_index_daily: 指数日线（如沪深300）
    - get_realtime_quote: 实时行情
    - get_moneyflow: 资金流向
    - get_stock_basic: 股票基本信息
    - get_limit_list: 涨跌停列表
    - get_top_list: 龙虎榜
    - get_financial_data: 财务指标
    - get_trade_cal: 交易日历
    """

    def __init__(self, token: str | None = None):
        self.token = token or TUSHARE_TOKEN
        data_mode = os.getenv("DATA_MODE", "websearch")

        # 仅在 JNB 模式下强制检查 API 配置
        if data_mode == "jnb":
            if not self.token:
                raise ValueError(
                    "JNB 模式下未设置 TUSHARE_TOKEN，请在 .env 中配置。\n或者将 DATA_MODE 改为 websearch。"
                )
            if not TUSHARE_API_URL:
                raise ValueError(
                    "JNB 模式下未设置 TUSHARE_API_URL，请在 .env 中配置中转 API 地址。\n"
                    "示例：TUSHARE_API_URL=https://tt.xiaodefa.cn"
                )

            # 初始化 Tushare SDK
            ts.set_token(self.token)
            self._pro = ts.pro_api()
            self._pro._DataApi__http_url = TUSHARE_API_URL
        else:
            self._pro = None

        try:
            from tushare.stock import cons as ct

            ct.verify_token_url = VERIFY_TOKEN_URL
        except Exception:
            pass

        self.min_request_interval = 0.55
        self.last_request_time = 0

    def _rate_limit(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)
        self.last_request_time = time.time()

    def _request_cn(self, endpoint: str, *, raise_on_error: bool = True, **params) -> pd.DataFrame | None:
        """CN核心读取不吞异常；旧查询可显式保留返回None的兼容行为。"""
        try:
            if self._pro is None:
                raise TushareDataError(endpoint, "config")
            self._rate_limit()
            result = getattr(self._pro, endpoint)(**params)
            if not isinstance(result, pd.DataFrame):
                raise TushareDataError(endpoint, "invalid_response")
            return result
        except Exception as exc:
            if isinstance(exc, TushareDataError):
                error = exc
            else:
                from api.services.sync_service import classify_error
                error = TushareDataError(endpoint, classify_error(exc))
            if raise_on_error:
                raise error from None
            logger.error("%s", error)
            return None

    def get_raw_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """原始个股日线，保持源端手/千元单位，由CN适配入口统一转换。"""
        return self._request_cn("daily", ts_code=ts_code, start_date=start_date, end_date=end_date)

    def get_adj_factor(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """逐日复权因子，不能以空值冒充无需复权。"""
        return self._request_cn("adj_factor", ts_code=ts_code, start_date=start_date, end_date=end_date)

    def get_index_weight(self, start_date: str, end_date: str, *, index_code: str = "399300.SZ") -> pd.DataFrame:
        """读取成分权重；完整性与时效由名单适配器按单日快照验证。"""
        return self._request_cn("index_weight", index_code=index_code, start_date=start_date, end_date=end_date)

    def get_stk_limit(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """原始涨跌停价格；辅助权限失败由上层标记规则未知。"""
        return self._request_cn("stk_limit", ts_code=ts_code, start_date=start_date, end_date=end_date)

    def get_suspend_d(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """停牌证据采用官方逐日接口；日内停牌不能解释全天缺少行情。"""
        return self._request_cn("suspend_d", ts_code=ts_code, start_date=start_date,
                                end_date=end_date, suspend_type="S")

    def get_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        """获取日线行情（个股，前复权）"""
        self._rate_limit()
        try:
            return ts.pro_bar(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
                adj="qfq",
                api=self._pro,
            )
        except Exception as e:
            logger.error(f"get_daily 失败: {e}")
            return None

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str, *, raise_on_error: bool = False) -> pd.DataFrame | None:
        """获取指数日线（如沪深300）；CN发布使用严格错误契约。"""
        return self._request_cn("index_daily", raise_on_error=raise_on_error,
                                ts_code=ts_code, start_date=start_date, end_date=end_date)

    def get_realtime_quote(self, ts_codes: list[str]) -> pd.DataFrame | None:
        """获取 A 股实时行情"""
        self._rate_limit()
        try:
            ts_code_str = ",".join(ts_codes)
            return ts.realtime_quote(ts_code=ts_code_str)
        except Exception as e:
            logger.error(f"get_realtime_quote 失败: {e}")
            return None

    def get_moneyflow(self, ts_code: str, trade_date: str) -> pd.DataFrame | None:
        """获取个股资金流向"""
        self._rate_limit()
        try:
            return self._pro.moneyflow(ts_code=ts_code, trade_date=trade_date)
        except Exception as e:
            logger.error(f"get_moneyflow 失败: {e}")
            return None

    def get_stock_basic(self, ts_code: str | None = None, name: str | None = None, *,
                        list_status: str = "L", raise_on_error: bool = False) -> pd.DataFrame | None:
        """基本资料兼容上市/退市/暂停状态，避免漏掉旧库标的。"""
        if list_status not in {"L", "D", "P"}:
            raise ValueError("上市状态只支持L/D/P")
        params = {"list_status": list_status}
        if ts_code:
            params["ts_code"] = ts_code
        if name:
            params["name"] = name
        return self._request_cn("stock_basic", raise_on_error=raise_on_error, **params)

    def get_limit_list(self, trade_date: str) -> pd.DataFrame | None:
        """获取涨跌停列表"""
        self._rate_limit()
        try:
            return self._pro.limit_list_d(trade_date=trade_date)
        except Exception as e:
            logger.error(f"get_limit_list 失败: {e}")
            return None

    def get_top_list(self, trade_date: str) -> pd.DataFrame | None:
        """获取龙虎榜数据"""
        self._rate_limit()
        try:
            return self._pro.top_list(trade_date=trade_date)
        except Exception as e:
            logger.error(f"get_top_list 失败: {e}")
            return None

    def get_financial_data(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        """获取财务指标"""
        self._rate_limit()
        try:
            return self._pro.fina_indicator(ts_code=ts_code, start_date=start_date, end_date=end_date)
        except Exception as e:
            logger.error(f"get_financial_data 失败: {e}")
            return None

    def get_trade_cal(self, exchange: str = "SSE", start_date: str = "", end_date: str = "", *,
                      raise_on_error: bool = False) -> pd.DataFrame | None:
        """获取交易日历；CN严格调用无法取得时阻止发布。"""
        params = {"exchange": exchange}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return self._request_cn("trade_cal", raise_on_error=raise_on_error, **params)

    def check_connection(self) -> bool:
        """检查 API 连通性"""
        df = self.get_daily("000001.SZ", "20250508", "20250515")
        return df is not None and len(df) > 0


# 测试
if __name__ == "__main__":
    import sys
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    logging.basicConfig(level=logging.INFO)

    client = TushareClient()
    print("=" * 50)
    print("Tushare 中转 API 连通性测试")
    print("=" * 50)

    if client.check_connection():
        print("[PASS] 连通性测试通过")
    else:
        print("[FAIL] 连通性测试失败")

    print("\n=== 平安银行 (000001.SZ) 日线 ===")
    df = client.get_daily("000001.SZ", "20250508", "20250515")
    if df is not None and len(df) > 0:
        print(df[["trade_date", "open", "high", "low", "close", "pct_chg"]].to_string(index=False))
    else:
        print("无数据")

    print("\n=== 沪深300 (000300.SH) 指数日线 ===")
    df2 = client.get_index_daily("000300.SH", "20250508", "20250515")
    if df2 is not None and len(df2) > 0:
        print(df2[["trade_date", "open", "high", "low", "close", "pct_chg"]].to_string(index=False))
    else:
        print("无数据")

    print("\n=== 实时行情 ===")
    df3 = client.get_realtime_quote(["000300.SH", "000001.SZ"])
    if df3 is not None and len(df3) > 0:
        print(df3[["TS_CODE", "NAME", "PRICE", "HIGH", "LOW", "VOLUME"]].to_string(index=False))
    else:
        print("无数据")
