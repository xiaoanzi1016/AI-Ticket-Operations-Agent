# -*- coding: utf-8 -*-
"""系统路由：健康检查与运行指标（不放在 /api/v1 下）。

大白话：这两个接口是给"运维"看的，不是给业务用的 ——
        - /health  回答"服务还活着吗"，给 K8s / docker healthcheck / 负载均衡用；
        - /metrics 回答"现在忙不忙"，给演示和排查用。
所以它们不带版本号前缀：探活地址应当稳定，不该随 API 版本变化。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.api.deps import get_db, get_settings, get_worker
from src.api.models import HealthResponse, MetricsResponse
from src.api.settings import ApiSettings
from src.api.worker import TaskWorker
from src.logger import log
from src.persistence import crud

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="健康检查")
def health(db: Session = Depends(get_db),
           cfg: ApiSettings = Depends(get_settings)) -> HealthResponse:
    """存活探针。

    大白话：不光回一句"我还活着"，还顺手摸一下数据库 —— 数据库连不上时服务
    其实是不可用的，回 healthy 会把流量引进一个处理不了请求的进程。

    技术细节：`SELECT 1` 是最轻量的"连接是否可用"验证，不读任何业务表。
    探活失败**不抛异常**（抛了调用方拿到的是 500，语义上不好区分"服务挂了"和
    "接口报错"），而是返回 status="degraded"，由编排系统按需处理。
    """
    try:
        db.execute(text("SELECT 1"))
        status = "healthy"
    except Exception as e:  # pragma: no cover - 只有库挂了才会走到
        log.error("健康检查：数据库不可用: %s", e)
        status = "degraded"
    return HealthResponse(status=status, version=cfg.api_version)


@router.get("/metrics", response_model=MetricsResponse, summary="运行指标")
def metrics(db: Session = Depends(get_db),
            worker: TaskWorker = Depends(get_worker)) -> MetricsResponse:
    """队列与处理量指标。

    大白话：看一眼"队列里堆了多少单、手上有几单在处理、今天处理了多少"。
    """
    stats = worker.stats
    try:
        today = crud.get_stats_today(db)
    except Exception as e:  # pragma: no cover - 统计失败不该让指标接口 500
        log.warning("指标：读取当日统计失败: %s", e)
        today = {}
    return MetricsResponse(today=today, **stats)
