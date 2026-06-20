# -*- coding: utf-8 -*-
"""任务路由：提交 / 查询详情 / 列表 / 取消。

【这个文件是干什么的】
对外提供 4 个业务接口，是"人"和"Agent"之间的接头处：

  POST /api/v1/tasks/submit           提交任务（可选带 Excel 附件）→ 立刻返回 task_id
  GET  /api/v1/tasks/recent           分页看最近任务
  GET  /api/v1/tasks/{task_id}        看某个任务的完整执行链路
  POST /api/v1/tasks/{task_id}/cancel 取消任务

【核心设计：接口只"接单"，不"做菜"】
提交接口绝不在这里跑 Agent —— 那会把事件循环卡死几十秒。它只做四件事：
落文件 → 建任务记录（processing）→ 丢进队列 → 返回 task_id。
真正的处理由 `worker.py` 的后台协程在线程池里完成，状态从库里的记录读。

【路由顺序有个坑】
`/recent` 必须写在 `/{task_id}` **前面**。FastAPI 按注册顺序匹配，
否则 `/recent` 会被 `/{task_id}` 抢先匹配，被当成 task_id="recent" 去查库。
"""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import ValidationError
from sqlalchemy.orm import Session

from src.api.dataset import UploadRejected, cleanup, save_uploads
from src.api.deps import get_db, get_worker
from src.api.models import (
    CancelResponse,
    TaskDetailResponse,
    TaskListResponse,
    TaskResponse,
    TaskSubmitRequest,
    from_task,
    to_list_item,
)
from src.api.worker import TaskJob, TaskWorker
from src.logger import log
from src.persistence import crud

router = APIRouter(prefix="/tasks", tags=["tasks"])

# 任务已结束（终态）的三种状态：不能再取消，也不会再变化
_TERMINAL_STATUSES: set[str] = {"completed", "failed", "cancelled"}


def _bad_request(code: str, message: str) -> HTTPException:
    """构造一个携带机器可读错误码的 4xx 异常。

    技术细节：detail 用 dict 而不是字符串，是为了让全局异常处理器能原样保留
    code —— 前端可以按 code 做分支（如 code="payload_too_large" 就提示用户压缩文件），
    而不是去正则匹配中文提示语。
    """
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                         detail={"code": code, "message": message})


# ----------------------------------------------------------------------
# 1) 提交任务
# ----------------------------------------------------------------------
@router.post("/submit", response_model=TaskResponse, summary="提交任务（异步处理）")
async def submit_task(
    user_input: Annotated[str, Form(description="自然语言任务描述")],
    customer: Annotated[str, Form(description="客户名 / 会话隔离键")] = "访客",
    delivery: Annotated[UploadFile | None,
                        File(description="发货订单 Excel（可选，会驱动本次查询）")] = None,
    # 注意：表单字段名必须叫 return，但 return 是 Python 关键字，不能当参数名，
    # 所以参数叫 return_order、用 alias 把表单字段名对齐回 return。
    return_order: Annotated[UploadFile | None,
                            File(alias="return", description="退货订单 Excel（可选）")] = None,
    warehouse: Annotated[UploadFile | None,
                         File(description="仓库退货订单 Excel（可选）")] = None,
    db: Session = Depends(get_db),
    worker: TaskWorker = Depends(get_worker),
) -> TaskResponse:
    """提交一条工单诉求，立刻返回任务编号（真正的处理在后台）。

    大白话：这叫"取号排队"—— 你把诉求和材料交上来，我回你一个号，
    你随时可以用这个号来查进度，不用在窗口干等。
    """
    # ① 文本字段体检（长度/非空由 pydantic 负责，出错转成 400 而不是 500）
    try:
        payload = TaskSubmitRequest(user_input=user_input, customer=customer)
    except ValidationError as e:
        first = e.errors()[0]
        raise _bad_request("invalid_field",
                           f"字段 {first.get('loc')} 不合法：{first.get('msg')}") from e

    # ② 附件落盘（体积/类型/格式问题在这里一次性拦掉）
    dataset = None
    try:
        dataset = await save_uploads(delivery, return_order, warehouse)
    except UploadRejected as e:
        raise HTTPException(status_code=e.status_code,
                            detail={"code": e.code, "message": str(e)}) from e

    # ③ 建任务记录：先落库再入队，保证"只要返回了 task_id，就一定查得到"
    try:
        task = crud.create_task(db, payload.user_input)
        if dataset is not None:
            # 附件也留一笔流水：审计要能回答"这一单当时带了什么材料"
            crud.create_tool_execution(
                db,
                task_id=task.task_id,
                tool_name="file_upload",
                input_params={"files": [f.to_dict() for f in dataset.files]},
                status="success",
                output_result={"drives_query": dataset.drives_query,
                               "note": "上传文件仅本次任务生效，处理完成后自动清理"},
            )
    except Exception as e:
        cleanup(dataset)   # 任务没建成，材料别留在磁盘上
        log.exception("提交任务：建任务记录失败")
        raise HTTPException(status_code=500,
                            detail={"code": "task_create_failed",
                                    "message": f"创建任务失败：{e}"}) from e

    # ④ 入队。队列满了就明确回 503（让调用方稍后重试），而不是无限堆内存
    job = TaskJob(task_id=task.task_id, user_input=payload.user_input,
                  customer=payload.customer, dataset=dataset)
    try:
        await worker.submit(job)
    except asyncio.QueueFull:
        cleanup(dataset)
        crud.update_task_status(db, task.task_id, "failed",
                                result_summary={"error": "任务队列已满，任务未被执行"})
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": "queue_full",
                                    "message": "任务队列已满，请稍后重试"}) from None

    log.info("任务已提交 task_id=%s customer=%s 附件=%d",
             task.task_id, payload.customer, len(dataset.files) if dataset else 0)
    return TaskResponse(
        task_id=task.task_id,
        status=task.status,
        created_at=task.created_at,
        message="任务已提交，正在处理中。可用 GET /api/v1/tasks/{task_id} 查询进度。",
    )


# ----------------------------------------------------------------------
# 2) 最近任务列表（必须声明在 /{task_id} 之前，见模块头注释）
# ----------------------------------------------------------------------
@router.get("/recent", response_model=TaskListResponse, summary="查询最近任务列表")
def list_recent_tasks(
    limit: Annotated[int, Query(ge=1, le=100, description="本页条数")] = 10,
    offset: Annotated[int, Query(ge=0, description="跳过的条数（分页用）")] = 0,
    db: Session = Depends(get_db),
) -> TaskListResponse:
    """分页返回最近任务，按创建时间倒序。"""
    items = crud.get_tasks_page(db, limit=limit, offset=offset)
    total = crud.count_tasks(db)
    return TaskListResponse(total=total, limit=limit, offset=offset,
                            items=[to_list_item(t) for t in items])


# ----------------------------------------------------------------------
# 3) 任务详情（含完整执行链路）
# ----------------------------------------------------------------------
@router.get("/{task_id}", response_model=TaskDetailResponse, summary="查询任务详情")
def get_task_detail(task_id: str, db: Session = Depends(get_db)) -> TaskDetailResponse:
    """查一个任务的详情 + 它执行过的每一次工具调用 / 闸门判定。"""
    task = crud.get_task_by_id(db, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "task_not_found",
                                    "message": f"任务 {task_id} 不存在"})
    executions = crud.get_tool_executions_by_task(db, task_id)
    return from_task(task, executions)


# ----------------------------------------------------------------------
# 4) 取消任务
# ----------------------------------------------------------------------
@router.post("/{task_id}/cancel", response_model=CancelResponse, summary="取消任务")
def cancel_task(task_id: str,
                db: Session = Depends(get_db),
                worker: TaskWorker = Depends(get_worker)) -> CancelResponse:
    """请求取消一个还没跑完的任务。

    大白话：取消是"协作式"的 —— 已经跑起来的任务没法从外部硬掐死，
    Agent 会在下一个检查点（每轮调用大模型之前）自己停下来。
    """
    task = crud.get_task_by_id(db, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "task_not_found",
                                    "message": f"任务 {task_id} 不存在"})

    if task.status in _TERMINAL_STATUSES:
        # 409 Conflict：语义上就是"当前状态不允许这个操作"，比 400 更准确
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "task_not_cancellable",
                    "message": f"任务已处于终态（{task.status}），无法取消"},
        )

    when = worker.request_cancel(task_id)
    if when == "running":
        # 正在执行：标志位已置起，但当前这一步（LLM 调用/工具执行）无法中断
        return CancelResponse(
            task_id=task_id, status="cancelled",
            message="取消指令已下达：将在当前步骤结束后生效（协作式取消）",
        )

    # 排队中，或状态是 processing 但已不在本 Worker 队列里（例如服务重启前遗留的任务）
    note = ("任务已取消" if when == "queued"
            else "任务不在当前队列中（可能由上次服务实例遗留），已标记为取消")
    crud.update_task_status(db, task_id, "cancelled", result_summary={"error": note})
    log.info("任务已取消 task_id=%s (when=%s)", task_id, when)
    return CancelResponse(task_id=task_id, status="cancelled", message=note)
