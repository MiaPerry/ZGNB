"""股票分析路由"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from api.models.stock import (
    StockAnalysisResponse,
    KlineChartResponse,
    FullIndicatorResponse,
    StockListResponse,
    StockMarket,
    StockSort,
    StockImportRequest,
    StockImportSnapshot,
)
from api.services import stock_service
from api.services.stock_import_service import get_stock_import_service
from modules.market_sync_lock import SyncBusyError

router = APIRouter()


@router.get("/list", response_model=StockListResponse)
def list_stocks(
    market: StockMarket = "美股",
    q: str = Query(default="", max_length=100),
    industry: str | None = Query(default=None, max_length=100),
    data_status: Literal["all", "available", "missing"] = "all",
    sort_by: StockSort = "ts_code",
    order: Literal["asc", "desc"] = "asc",
    page: int = Query(default=1, ge=1, le=1000000),
    page_size: int = Query(default=25, ge=1, le=100),
):
    """浏览系统收录的股票及最近日线行情，空行业参数表示未分类。"""
    return stock_service.list_stocks(
        market=market, q=q, industry=industry, data_status=data_status,
        sort_by=sort_by, order=order, page=page, page_size=page_size,
    )


def _import_call(action):
    try:
        return action()
    except SyncBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="股票添加任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/imports", response_model=StockImportSnapshot, status_code=202)
def import_stocks(body: StockImportRequest):
    """提交单只/批量普通美股收录任务，不在请求线程下载行情。"""
    return _import_call(lambda: get_stock_import_service().submit(body.codes))


@router.get("/imports/latest", response_model=StockImportSnapshot)
def latest_stock_import():
    return _import_call(lambda: get_stock_import_service().get_status())


@router.get("/imports/{task_id}", response_model=StockImportSnapshot)
def stock_import_status(task_id: str):
    return _import_call(lambda: get_stock_import_service().get_status(task_id))


@router.post("/imports/{task_id}/retry", response_model=StockImportSnapshot, status_code=202)
def retry_stock_import(task_id: str):
    return _import_call(lambda: get_stock_import_service().retry(task_id))


@router.get("/analyze/{ts_code}", response_model=StockAnalysisResponse)
def analyze_stock(ts_code: str, days: int = Query(default=120, ge=10, le=1000)):
    """全量分析：指标 + 战法 + 评分 + 诊断"""
    try:
        return stock_service.get_full_analysis(ts_code, days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分析失败: {e}")


@router.get("/analyze/{ts_code}/klines", response_model=KlineChartResponse)
def get_klines(ts_code: str, days: int = Query(default=120, ge=10, le=1000)):
    """获取 K 线图表数据（ECharts 列式格式）"""
    try:
        return stock_service.get_kline_chart_data(ts_code, days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取 K 线失败: {e}")


@router.get("/analyze/{ts_code}/signals")
def get_signals(ts_code: str, days: int = Query(default=120, ge=10, le=1000)):
    """获取战法信号列表"""
    try:
        return stock_service.get_signals(ts_code, days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取信号失败: {e}")


@router.get("/score/{ts_code}")
def get_score(ts_code: str):
    """获取综合评分"""
    try:
        return stock_service.get_score(ts_code)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取评分失败: {e}")
