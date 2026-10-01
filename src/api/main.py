# -*- coding: utf-8 -*-
"""FastAPI 应用入口（PHASE 2）。

启动方式：
    uvicorn src.api.main:app --host 0.0.0.0 --port 8000
    python scripts/run_api.py                 # 等价，且支持 --reload / --port 等参数

【这个文件负责什么】
只做"应用装配"，不写业务逻辑：
1. 建 FastAPI 实例（标题/版本/描述 → 自动生成 /docs）
2. 装 CORS 中间件（方便后续接前端）
3. 挂载两个路由模块（system 不带前缀、tasks 挂 /api/v1）
4. 注册全局异常处理器（保证任何错误都是统一的 JSON 形状）
5. 用 lifespan 管生命周期：启动时建表 + 拉起后台 Worker，关闭时优雅停掉

【为什么用 lifespan 而不是 @app.on_event("startup")】
`on_event` 在 FastAPI 里已标记为过时；lifespan 是标准的上下文管理器写法，
`yield` 之前是启动、之后是关闭，资源成对出现，不容易漏（漏关 Worker 会让
进程无法正常退出）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.auth import warn_if_auth_disabled
from src.api.deps import get_worker
from src.api.models import ErrorResponse
from src.api.routers import system, tasks
from src.api.settings import api_settings
from src.logger import log
from src.persistence.database import DB_PATH, init_db

# HTTP 状态码 -> 默认错误码（调用方没显式给 code 时用它兜底）
_DEFAULT_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    500: "internal_error",
    503: "service_unavailable",
}


def _error_payload(code: str, message: str) -> dict[str, Any]:
    """统一错误响应体。所有出错路径都套这一个壳，前端只需写一份错误处理。"""
    return {"error": {"code": code, "message": message}}


# ----------------------------------------------------------------------
# 生命周期
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用启停钩子。

    大白话：开门营业前把账本备好（建表）、把柜员叫上岗（Worker）；
            打烊时先让柜员把手上的活收尾（停 Worker），再关灯。
    """
    init_db()
    log.info("API 启动中：db=%s", DB_PATH)
    # 没配 API_AUTH_TOKEN 时明确告警（见 src/api/auth.py 的说明）
    warn_if_auth_disabled()
    worker = get_worker()
    await worker.start()
    log.info("API 已就绪：workers=%d mock=%s", api_settings.workers,
             api_settings.agent_force_mock)
    try:
        yield
    finally:
        await worker.stop()
        log.info("API 已关闭")


# ----------------------------------------------------------------------
# 应用实例
# ----------------------------------------------------------------------
app = FastAPI(
    title=api_settings.api_title,
    version=api_settings.api_version,
    description=(
        "把「智能工单售后 Agent」包装成 HTTP 服务。\n\n"
        "**处理是异步的**：`POST /api/v1/tasks/submit` 只负责收单并返回 `task_id`，"
        "真正的 Agent 处理在后台队列里进行，用 `GET /api/v1/tasks/{task_id}` 轮询结果。"
    ),
    lifespan=lifespan,
)

# CORS：允许跨域，方便后续接前端页面或第三方系统。
# 注意 allow_credentials 与 "*" 不能同时开（浏览器会直接拒绝），见 ApiSettings 里的说明。
app.add_middleware(
    CORSMiddleware,
    allow_origins=api_settings.cors_origin_list,
    allow_credentials=api_settings.allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 路由：system 不带版本前缀（探活地址要稳定），tasks 挂到 /api/v1
app.include_router(system.router)
app.include_router(tasks.router, prefix="/api/v1")


@app.get("/", include_in_schema=False)
def root() -> dict[str, Any]:
    """根路径：给个"这是哪儿、去哪儿看文档"的提示。"""
    return {
        "service": api_settings.api_title,
        "version": api_settings.api_version,
        "docs": "/docs",
        "health": "/health",
        "submit": "POST /api/v1/tasks/submit",
    }


# ----------------------------------------------------------------------
# 全局异常处理：保证"任何错误都是同一个 JSON 形状"
# ----------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request,
                                 exc: StarletteHTTPException) -> JSONResponse:
    """处理主动抛出的 HTTPException（404 任务不存在、409 不能取消等）。

    大白话：业务代码只管抛"错在哪儿"，由这里统一翻译成对外的报文格式。
    """
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code") or _DEFAULT_CODES.get(exc.status_code, "http_error"))
        message = str(detail.get("message") or detail)
    else:
        code = _DEFAULT_CODES.get(exc.status_code, "http_error")
        message = str(detail)
    return JSONResponse(status_code=exc.status_code,
                        content=_error_payload(code, message),
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request,
                                       exc: RequestValidationError) -> JSONResponse:
    """处理入参校验失败（缺字段、类型不对、超出范围）。

    大白话：把 pydantic 那一长串英文报错，压成一句能看懂的"哪个字段错在哪"。
    """
    problems: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        problems.append(f"{loc or '请求体'}: {err.get('msg')}")
    return JSONResponse(
        status_code=422,
        content=_error_payload("validation_error", "；".join(problems) or "请求参数不合法"),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：任何没被处理的异常。

    大白话：内部到底怎么炸的写进日志给运维看，对外只回一句"服务器内部错误" ——
    堆栈信息里可能有路径、配置等敏感内容，不该返回给调用方。
    """
    log.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=_error_payload("internal_error",
                               "服务器内部错误，请稍后重试或联系管理员"),
    )


__all__ = ["app"]
