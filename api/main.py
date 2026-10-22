#!/usr/bin/env python3
"""Z哥量化 API — FastAPI 入口"""

import logging
import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.config import settings
from modules.database import MarketCodeError, market_context, validate_market_code

logger = logging.getLogger("zettaranc-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动 / 关闭生命周期"""
    logger.info("Z哥量化 API 启动 (mode=%s, db=%s)", settings.data_mode, settings.db_path)
    yield
    logger.info("Z哥量化 API 关闭")


async def validate_request_codes(request: Request):
    """在路由执行前校验标准代码，避免错误市场进入查询/写入/付费调用。"""
    def check(value):
        try:
            validate_market_code(value)
        except MarketCodeError as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "ts_code":
                    check(child)
                elif key == "ts_codes" and isinstance(child, list):
                    for code in child:
                        check(code)
                elif isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    if "ts_code" in request.path_params:
        check(request.path_params["ts_code"])
    for value in request.query_params.getlist("ts_code"):
        check(value)
    if request.method in ("POST", "PUT", "PATCH") and "application/json" in request.headers.get("content-type", ""):
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return  # 由FastAPI模型校验提供格式错误。
        walk(body)


app = FastAPI(
    title="Z哥量化 API",
    description="zettaranc 量化交易工具 REST API",
    version="1.0.0",
    lifespan=lifespan,
    dependencies=[Depends(validate_request_codes)],
)

# CORS
origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def bind_request_market(request: Request, call_next):
    """请求边界捕获库身份，退出时恢复；不修改全局环境变量。"""
    if not request.url.path.startswith(settings.api_prefix) or request.method == "OPTIONS":
        return await call_next(request)
    selected = request.headers.get("X-Market", "US")
    try:
        with market_context(selected) as binding:
            request.state.workspace_market = binding.market
            if selected == "CN" and not binding.db_path.is_file() and not request.url.path.endswith("/system/health"):
                return JSONResponse(status_code=503, content={
                    "error": "market_unavailable", "detail": "CN尚未初始化", "workspace_market": "CN",
                })
            response = await call_next(request)
            response.headers["X-Market"] = binding.market
            return response
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})


# 全局异常处理
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("未处理异常: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": str(exc)},
    )


# ── 注册路由 ──
from api.routes.stock import router as stock_router
from api.routes.screen import router as screen_router
from api.routes.watchlist import router as watchlist_router
from api.routes.diagnosis import router as diagnosis_router
from api.routes.backtest import router as backtest_router
from api.routes.trade import router as trade_router
from api.routes.system import router as system_router
from api.routes.commentary import router as commentary_router

prefix = settings.api_prefix
app.include_router(stock_router, prefix=f"{prefix}/stock", tags=["stock"])
app.include_router(screen_router, prefix=f"{prefix}/screen", tags=["screen"])
app.include_router(watchlist_router, prefix=f"{prefix}/watchlist", tags=["watchlist"])
app.include_router(diagnosis_router, prefix=f"{prefix}/diagnosis", tags=["diagnosis"])
app.include_router(backtest_router, prefix=f"{prefix}/backtest", tags=["backtest"])
app.include_router(trade_router, prefix=f"{prefix}/trade", tags=["trade"])
app.include_router(system_router, prefix=f"{prefix}/system", tags=["system"])
app.include_router(commentary_router, prefix=f"{prefix}", tags=["commentary"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api.main:app", host="0.0.0.0", port=settings.api_port, reload=True)
