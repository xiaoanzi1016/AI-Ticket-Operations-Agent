# -*- coding: utf-8 -*-
"""API 请求 / 响应模型（Pydantic v2）。

【这个文件是干什么的】
定义 HTTP 接口"进出长什么样"。它有三个作用：
1. **校验**：进来的字段类型/长度不对，FastAPI 直接返回 422，不会把脏数据带进业务层；
2. **裁剪**：出去的数据只暴露该暴露的字段（比如 ORM 里的自增 id 就不需要全给前端）；
3. **文档**：`/docs` 里的字段说明、示例，全部由这里的 Field 描述自动生成。

【和 ORM 模型的关系】
`src/persistence/models.py` 里的 Task / ToolExecution 是**数据库表**（怎么存）；
这里的 *Response 是**HTTP 报文**（怎么传）。两者刻意分开：
以后改表结构不会连带把接口协议改掉，反之亦然。
转换逻辑统一收在本文件底部的 `from_task / from_execution`，别处不再手写映射。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.persistence.models import Task, ToolExecution


# ----------------------------------------------------------------------
# 内部小工具
# ----------------------------------------------------------------------
def _parse_json(raw: Optional[str]) -> Optional[dict]:
    """把库里存的 JSON 字符串还原成对象。

    大白话：数据库里 input_params 存的是一行文字，接口上要还给前端一个真正的对象。

    技术细节：Phase 1 刻意采用"超长截断在字符串层"的策略，所以存下来的内容
    **不保证仍是合法 JSON**。这里解析失败不报错，而是降级成 `{"_raw": 原文}` ——
    审计场景下"能看到前 10000 字"比"因为格式坏了整条记录取不出来"重要得多。
    """
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_value": value}


# ----------------------------------------------------------------------
# 请求模型
# ----------------------------------------------------------------------
class TaskSubmitRequest(BaseModel):
    """提交任务时的**文本字段**。

    为什么附件不在这里：multipart 里的文件由 FastAPI 的 `UploadFile` 直接接收
    （Pydantic 模型不参与文件流的解析）。本模型只负责文本字段的校验，
    在 `routers/tasks.py` 的 submit 里构造出来后即完成"入参体检"。
    """

    user_input: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="自然语言任务描述，如「客户反映订单 PO20260928-00001 少发一瓶玻璃水，要求补发」",
    )
    customer: str = Field(
        "访客",
        max_length=64,
        description="客户名 / 会话隔离键。同一客户的历史对话会被复用，不同客户互不串味",
    )


# ----------------------------------------------------------------------
# 响应模型
# ----------------------------------------------------------------------
class TaskResponse(BaseModel):
    """提交任务后的即时回执（此时任务只在队列里，还没开始处理）。"""

    task_id: str = Field(..., description="任务编号，后续查询详情都用它")
    status: str = Field(..., description="任务状态：processing / completed / failed / cancelled")
    created_at: datetime = Field(..., description="任务创建时间（ISO 8601）")
    message: str = Field(..., description="给用户的提示文案")


class ExecutionItem(BaseModel):
    """一次工具执行 / 安全闸门判定的流水。

    注意 status 有三种取值，其中 blocked 是"被安全闸门拦下"，
    它同样是有效结果 —— 审计最关心的恰恰是"当时为什么没做"。
    """

    id: int
    tool_name: str = Field(..., description="工具名，如 query_order；闸门判定统一为 safety_gate")
    status: str = Field(..., description="success / failed / blocked")
    executed_at: datetime
    input_params: Optional[dict] = Field(None, description="入参（JSON 还原；截断时降级为 _raw）")
    output_result: Optional[dict] = Field(None, description="出参（同上）")
    error_message: Optional[str] = Field(None, description="失败或被拦截的原因")


class TaskDetailResponse(BaseModel):
    """任务详情：任务本身 + 完整的执行链路。"""

    task_id: str
    user_input: str
    status: str
    created_at: datetime
    updated_at: datetime
    result_summary: Optional[str] = Field(
        None,
        description="结果摘要。库里存的是 JSON 字符串，这里按原样返回，"
                    "方便前端自行决定是解析展示还是直接显示。",
    )
    executions: list[ExecutionItem] = Field(
        default_factory=list, description="该任务的全部工具执行流水（按时间正序 = 当时的执行链路）"
    )


class TaskListItem(BaseModel):
    """列表里的一条任务（只给概览字段，不带流水，避免响应体过大）。"""

    task_id: str
    user_input: str
    status: str
    created_at: datetime


class TaskListResponse(BaseModel):
    """分页列表响应。"""

    total: int = Field(..., description="任务总数，前端据此算总页数")
    limit: int = Field(..., description="本页请求的条数")
    offset: int = Field(..., description="本页跳过的条数")
    items: list[TaskListItem] = Field(default_factory=list)


class CancelResponse(BaseModel):
    """取消任务的回执。"""

    task_id: str
    status: str = Field(..., description="取消后的目标状态：cancelled")
    message: str = Field(..., description="说明文案。若任务已在执行中，会提示取消将在下一步生效")


class HealthResponse(BaseModel):
    """健康检查响应。"""

    status: str = Field(..., description="healthy / degraded")
    version: str


class MetricsResponse(BaseModel):
    """运行指标（给运维/演示看，不做鉴权）。"""

    queue_size: int = Field(..., description="还在队列里排队、尚未开始处理的任务数")
    running: int = Field(..., description="正在执行的任务数")
    workers: int = Field(..., description="后台 Worker 协程数量")
    worker_alive: bool = Field(..., description="Worker 是否在运行")
    today: dict[str, Any] = Field(default_factory=dict, description="当日处理量统计")


class ErrorDetail(BaseModel):
    code: str = Field(..., description="机器可读的错误码，如 task_not_found")
    message: str = Field(..., description="给人看的错误说明")


class ErrorResponse(BaseModel):
    """全局统一错误格式：任何接口出错，响应体都是这个形状。"""

    error: ErrorDetail


# ----------------------------------------------------------------------
# ORM -> 响应模型 的转换
# ----------------------------------------------------------------------
def from_execution(exe: ToolExecution) -> ExecutionItem:
    """把一条工具流水 ORM 对象转成响应模型。"""
    return ExecutionItem(
        id=exe.id,
        tool_name=exe.tool_name,
        status=exe.status,
        executed_at=exe.executed_at,
        input_params=_parse_json(exe.input_params),
        output_result=_parse_json(exe.output_result),
        error_message=exe.error_message,
    )


def from_task(task: Task, executions: Optional[list[ToolExecution]] = None) -> TaskDetailResponse:
    """把任务 ORM 对象（可选带上流水）转成详情响应模型。"""
    return TaskDetailResponse(
        task_id=task.task_id,
        user_input=task.user_input,
        status=task.status,
        created_at=task.created_at,
        updated_at=task.updated_at,
        result_summary=task.result_summary,
        executions=[from_execution(e) for e in (executions or [])],
    )


def to_list_item(task: Task) -> TaskListItem:
    """把任务 ORM 对象转成列表项。"""
    return TaskListItem(
        task_id=task.task_id,
        user_input=task.user_input,
        status=task.status,
        created_at=task.created_at,
    )
