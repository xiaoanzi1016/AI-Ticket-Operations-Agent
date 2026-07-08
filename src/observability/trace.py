# -*- coding: utf-8 -*-
"""可观测层：Trace + 指标埋点。

记录每次调用的动作、工具、参数摘要、耗时、结果与审批人。
用于：排查 + 审计 + 喂给评测指标（成功率/拦截率/延迟/成本）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.logger import log


@dataclass
class TraceRecord:
    ticket_id: str
    step: str                    # 工具名 / 动作类型 / llm_call
    params: dict = field(default_factory=dict)
    result: Any = None
    ok: bool = True
    latency_ms: float = 0.0
    operator: str = "auto"       # auto / 人工ID
    decision: str = "allowed"    # allowed / denied / escalated / pending
    ts: float = field(default_factory=time.time)


class Tracer:
    """轻量 Trace 收集器。"""

    def __init__(self) -> None:
        self._records: list[TraceRecord] = []

    def record(self, **kwargs) -> TraceRecord:
        rec = TraceRecord(**kwargs)
        self._records.append(rec)
        log.info("TRACE | ticket=%s | step=%s | ok=%s | %.1fms | decision=%s",
                 kwargs.get("ticket_id"), kwargs.get("step"),
                 kwargs.get("ok"), kwargs.get("latency_ms", 0.0), kwargs.get("decision"))
        return rec

    def timer(self, ticket_id: str, step: str, **base) -> tuple[float, TraceRecord]:
        """上下文计时器用法：
        start, rec = tracer.timer(ticket, "query_order")
        ...执行...
        rec = tracer.finish(start, rec, result=..., ok=...)
        """
        start = time.time()
        rec = self.record(ticket_id=ticket_id, step=step, **base)
        return start, rec

    def finish(self, start: float, rec: TraceRecord, result=None, ok: bool = True,
               decision: str | None = None) -> TraceRecord:
        rec.latency_ms = (time.time() - start) * 1000
        rec.result = result
        rec.ok = ok
        if decision:
            rec.decision = decision
        return rec

    @property
    def records(self) -> list[TraceRecord]:
        return list(self._records)

    def offset(self) -> int:
        """取当前记录数作为游标（用于按次统计）。"""
        return len(self._records)

    def summary(self) -> dict:
        """汇总全部记录（累计口径，仅用于全局统计）。"""
        return self._summarize(self._records)

    def summary_since(self, offset: int) -> dict:
        """只汇总 offset 之后的记录 —— 单次 run 的真实指标。

        注意：不要用 summary() 当作单次运行指标，它会把历史记录一起算进去，
        导致 total_calls / avg_latency 随运行次数持续虚增。
        """
        return self._summarize(self._records[offset:])

    @staticmethod
    def _summarize(records: list[TraceRecord]) -> dict:
        total = len(records)
        ok = sum(1 for r in records if r.ok)
        denied = sum(1 for r in records if r.decision == "denied")
        avg_latency = sum(r.latency_ms for r in records) / total if total else 0.0
        return {
            "total_calls": total,
            "success_rate": (ok / total) if total else 0.0,
            "denied_count": denied,
            "avg_latency_ms": avg_latency,
        }


# 默认 Tracer（单进程演示用；多租户场景应由 AgentContext 注入独立实例）
tracer = Tracer()