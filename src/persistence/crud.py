# -*- coding: utf-8 -*-
"""持久化层：CRUD 读写函数（唯一数据库读写入口）。

【这个文件是干什么的】
把所有"怎么读写库"的知识集中在这一个文件里，上层（agent.py / query_cli.py）
只调用这些函数，不自己拼 SQL、不自己管事务。好处是：
1. 换库/换表结构时只改这里；
2. 每个函数都能被单独测；
3. 截断、序列化这类容易漏的细节只有一处实现。

【函数一览】
- create_task                    新建任务（生成 task_id）
- update_task_status             更新任务状态 / 结果摘要
- create_tool_execution          记一条工具执行流水
- get_task_by_id                 按 task_id 查任务
- get_recent_tasks               最近 N 条任务
- get_tasks_by_date              按日期（YYYY-MM-DD）查任务
- get_tool_executions_by_task    某任务的全部执行流水
- get_stats / get_stats_today    当日/指定日处理量统计（query_cli stats 用）
- get_tasks_page / count_tasks   PHASE 2 新增：分页列表 + 总数（Web 层 recent 接口用）

【业务数据 CRUD（迁库新增）】
上面一组是"运行台账"，下面一组是"业务底表"（orders / inventory / returns），
给数据层（src/data_source.py）当唯一的数据库入口：
- replace_orders / replace_inventory / replace_returns   迁移脚本批量写入
- count_orders / count_inventory / count_returns          迁移脚本统计校验
- get_order / get_inventory                              单条查询（替代 CSV 查找）
- list_orders                                            全量订单
- get_returns                                            条件查退货
- get_customer_return_rate                               某客户退货率（近 N 天）
- search_orders                                          按客户/门店模糊查（联表演示用）
- adjust_inventory                                       库存扣减（写回库）

【约定】
- 所有函数自带 `db.commit()`：调用方不用记得提交，拿到的对象已经是持久化状态。
- 所有函数只抛 SQLAlchemy 原生异常，**不吞异常**；"失败不阻断主流程"由上层
  （agent.py）用 try-except 兜底 —— 库挂了也不该让工单处理崩掉。
- 业务表的读写函数额外承担一个契约：**返回领域对象而不是 ORM 对象**
  （`Order` / `Return` dataclass）。这样上层代码完全不感知 SQLAlchemy 的存在，
  也才可能在库缺失时无缝降级回 CSV 实现。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from src.domain.models import Order as DomainOrder
from src.domain.models import Return as DomainReturn
from src.logger import log
from src.persistence.models import Inventory, Order, Return, Task, ToolExecution

# ----------------------------------------------------------------------
# 常量与内部工具
# ----------------------------------------------------------------------
# 单条字段最大存储字符数。超过就截断 —— 比如某个工具返回了整张表，
# 不截断会让单行文本把库撑大、CLI 也没法看。
MAX_STORE_CHARS: int = 10000
# 截断时追加的提示语（需求要求显式提示"结果已截断"）
TRUNCATED_TAG: str = "…[结果已截断]"


def _dumps(obj, max_chars: int = MAX_STORE_CHARS, field: str = "") -> str:
    """把任意对象安全序列化成 JSON 字符串，超长自动截断。

    大白话：dict/list 存进数据库前先压成一行文字；太长的剪掉并标注"已截断"。

    技术细节：
    - `ensure_ascii=False` 保留中文原样，库里可读、CLI 不用二次解码；
    - `default=str` 兜底：万一参数里混进了 datetime/自定义对象，不至于让整条
      记录因为 `TypeError: Object is not JSON serializable` 而丢失；
    - 截断发生在**字符串层**（不是 JSON 结构层），只保证不会再超长，
      不保证截断后仍是合法 JSON —— 这是刻意取舍：审计库里"看到前 10000 字"
      比"格式完整但什么都没存下"更有用。
    """
    if obj is None:
        return ""
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:  # pragma: no cover - 极少数不可序列化对象
        log.warning("序列化失败(%s)，降级为 repr 存储: %s", e, field)
        text = json.dumps({"_repr": repr(obj)}, ensure_ascii=False)
    if len(text) > max_chars:
        log.warning("字段 %s 超过 %d 字符，已截断存储", field or "(unknown)", max_chars)
        text = text[:max_chars] + TRUNCATED_TAG
    return text


def _now() -> datetime:
    """统一取"当前本地时间"，方便测试时替换/定位。"""
    return datetime.now()


def _new_task_id() -> str:
    """生成任务编号：`task_` + uuid4 十六进制前 16 位。

    大白话：像 "task_3f9a1c07be24d5e1" 这样一眼能认、又几乎不会重复的编号。
    技术细节：uuid4 是随机 UUID，取 16 个十六进制字符 = 64 bit 随机空间，
    在本项目的量级下碰撞概率可忽略；相比完整 36 位 UUID，日志里更短更好念。
    """
    return f"task_{uuid.uuid4().hex[:16]}"


def _day_range(date_str: str) -> tuple[datetime, datetime]:
    """把 'YYYY-MM-DD' 转成 [当天 00:00:00, 次日 00:00:00) 的时间区间。

    为什么用左闭右开区间而不是 `DATE(created_at) = ?`：
    前者能吃到 created_at 上的索引，后者对列做函数运算会让索引失效。
    """
    try:
        day = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError) as e:
        raise ValueError(f"日期格式应为 YYYY-MM-DD，收到: {date_str!r}") from e
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


# ----------------------------------------------------------------------
# Task CRUD
# ----------------------------------------------------------------------
def create_task(db: Session, user_input: str) -> Task:
    """创建新任务，返回 Task 对象。

    参数：
        db:         数据库会话（由 database.get_db() 提供）
        user_input: 用户原始输入，原样入库（审计要原文）

    返回：
        已持久化的 Task 对象（task_id 已生成，可直接读 task.task_id）

    异常：
        数据库异常原样抛出，交由上层 try-except 处理。
    """
    task = Task(
        task_id=_new_task_id(),
        user_input=user_input or "",
        status="processing",
    )
    db.add(task)
    db.commit()
    db.refresh(task)   # 回读数据库生成的 id / created_at，保证返回值字段完整
    log.info("持久化：新建任务 task_id=%s", task.task_id)
    return task


def update_task_status(db: Session, task_id: str, status: str,
                       result_summary: str | None = None) -> Task | None:
    """更新任务状态和结果摘要。

    参数：
        db:             数据库会话
        task_id:        任务编号
        status:         processing / completed / failed
        result_summary: 结果摘要。可传 dict/list（内部自动 JSON 序列化+截断），
                        也可直接传已序列化好的 JSON 字符串；传 None 表示不覆盖。

    返回：
        更新后的 Task；task_id 不存在时返回 None（并打 WARNING，不抛异常 ——
        "更新不到" 属于业务上可接受的失败，不该打断主流程）。

    异常：
        数据库异常原样抛出（由上层兜底）。
    """
    task = get_task_by_id(db, task_id)
    if task is None:
        log.warning("持久化：任务不存在，忽略状态更新 task_id=%s", task_id)
        return None

    task.status = status
    if result_summary is not None:
        # 已经是对接好的 JSON 字符串就直接用，避免二次转义成 "\"{...}\""
        if isinstance(result_summary, str):
            task.result_summary = result_summary
        else:
            task.result_summary = _dumps(result_summary, field="task.result_summary")
    task.updated_at = _now()
    db.commit()
    db.refresh(task)
    log.info("持久化：任务状态更新 task_id=%s -> %s", task_id, status)
    return task


# ----------------------------------------------------------------------
# ToolExecution CRUD
# ----------------------------------------------------------------------
def create_tool_execution(db: Session, task_id: str, tool_name: str,
                          input_params: dict, status: str,
                          output_result: dict | None = None,
                          error_message: str | None = None) -> ToolExecution:
    """记录一次工具执行。

    参数：
        db:            数据库会话
        task_id:       所属任务编号（必须已存在，否则外键约束会拒绝）
        tool_name:     工具/环节名，如 "query_order"、"safety_gate"
        input_params:  输入参数字典（内部序列化为 JSON）
        status:        执行状态：success / failed / blocked
        output_result: 输出结果字典，可空（序列化后超过 10000 字符会截断）
        error_message: 失败/被拦截原因，可空

    返回：
        已持久化的 ToolExecution 对象

    异常：
        数据库异常原样抛出（由上层兜底）。
    """
    exe = ToolExecution(
        task_id=task_id,
        tool_name=tool_name or "unknown",
        input_params=_dumps(input_params, field=f"{tool_name}.input_params") or "{}",
        output_result=_dumps(output_result, field=f"{tool_name}.output_result") or None,
        status=status,
        error_message=error_message,
    )
    db.add(exe)
    db.commit()
    db.refresh(exe)
    log.info("持久化：工具流水 task_id=%s tool=%s status=%s", task_id, tool_name, status)
    return exe


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------
def get_task_by_id(db: Session, task_id: str) -> Task | None:
    """根据 task_id 查询任务详情；不存在返回 None。

    用 `scalar_one_or_none()` 而不是 `first()`：task_id 有唯一索引，
    理论上最多一条；若真出现多条说明数据被污染，这里会直接报错而不是静默取第一条。
    """
    stmt = select(Task).where(Task.task_id == task_id)
    return db.execute(stmt).scalar_one_or_none()


def get_recent_tasks(db: Session, limit: int = 10) -> list[Task]:
    """获取最近 N 条任务，按创建时间倒序。

    同一秒内创建的多条任务再按自增 id 倒序，保证顺序稳定（不会因时间戳相同而乱序）。
    """
    try:
        n = max(1, int(limit))
    except (TypeError, ValueError):
        n = 10
    stmt = select(Task).order_by(Task.created_at.desc(), Task.id.desc()).limit(n)
    return list(db.execute(stmt).scalars().all())


def get_tasks_by_date(db: Session, date_str: str) -> list[Task]:
    """按日期查询任务（date_str 格式：YYYY-MM-DD），按创建时间倒序。

    异常：
        ValueError —— 日期格式非法（由调用方转成友好提示）。
    """
    start, end = _day_range(date_str)
    stmt = (
        select(Task)
        .where(Task.created_at >= start, Task.created_at < end)
        .order_by(Task.created_at.desc(), Task.id.desc())
    )
    return list(db.execute(stmt).scalars().all())


def get_tool_executions_by_task(db: Session, task_id: str) -> list[ToolExecution]:
    """查询某个任务的所有工具执行记录，按执行时间正序（= 当时的执行链路）。"""
    stmt = (
        select(ToolExecution)
        .where(ToolExecution.task_id == task_id)
        .order_by(ToolExecution.executed_at.asc(), ToolExecution.id.asc())
    )
    return list(db.execute(stmt).scalars().all())


# ----------------------------------------------------------------------
# PHASE 2: 分页查询（Web 层 /api/v1/tasks/recent 用）
# 大白话：Web 接口要"第几页、每页几条"，而 get_recent_tasks 只会"给我最近 N 条"。
#         这里补两个函数，一个取某一段、一个数总数（前端要算总页数）。
# 为什么不改造 get_recent_tasks：它在 Phase 1 已被 query_cli 与测试使用，
#         保持原样最安全 —— 新增函数比改老函数的风险低得多。
# ----------------------------------------------------------------------
def get_tasks_page(db: Session, limit: int = 10, offset: int = 0) -> list[Task]:
    """分页取任务列表，按创建时间倒序（同一秒再按自增 id 倒序，保证顺序稳定）。

    参数：
        limit:  本页条数（自动夹在 1..1000，防止调用方传负数或超大值）
        offset: 跳过前多少条（负数按 0 处理）

    技术细节：用 SQL 的 LIMIT/OFFSET 在库里分页，而不是把全部记录拉到内存再切片
    —— 数据量涨上来后内存占用是常数级。
    """
    try:
        n = min(1000, max(1, int(limit)))
    except (TypeError, ValueError):
        n = 10
    try:
        skip = max(0, int(offset))
    except (TypeError, ValueError):
        skip = 0
    stmt = (
        select(Task)
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(n)
        .offset(skip)
    )
    return list(db.execute(stmt).scalars().all())


def count_tasks(db: Session) -> int:
    """任务总数（配合 get_tasks_page，供前端计算总页数）。

    技术细节：用 `select(func.count())` 让数据库自己数，不把行拉回应用层。
    """
    return int(db.execute(select(func.count()).select_from(Task)).scalar_one())


# ----------------------------------------------------------------------
# 统计（query_cli stats 用）
# ----------------------------------------------------------------------
def get_stats(db: Session, date_str: str) -> dict:
    """统计指定日期的处理量。

    返回：
        {
          "date": "2026-09-30",
          "total": 12,                       # 任务总数
          "by_status": {"completed": 10, "failed": 1, "processing": 1},
          "tool_calls": 34,                  # 工具执行流水条数
          "blocked": 5,                      # 被安全闸门拦截的次数
          "tool_failed": 2,                  # 工具自身报错次数
        }

    技术细节：用 SQL 的 `group by` 在库里算，而不是把当天全部记录拉到内存里数
    —— 数据量涨上来后前者是常数级内存。
    """
    start, end = _day_range(date_str)
    window = (Task.created_at >= start, Task.created_at < end)

    total = db.execute(select(func.count()).select_from(Task).where(*window)).scalar_one()
    by_status = {
        status: cnt
        for status, cnt in db.execute(
            select(Task.status, func.count()).where(*window).group_by(Task.status)
        ).all()
    }
    tool_window = (ToolExecution.executed_at >= start, ToolExecution.executed_at < end)
    tool_calls = db.execute(
        select(func.count()).select_from(ToolExecution).where(*tool_window)
    ).scalar_one()
    blocked = db.execute(
        select(func.count()).select_from(ToolExecution).where(
            *tool_window, ToolExecution.status == "blocked")
    ).scalar_one()
    tool_failed = db.execute(
        select(func.count()).select_from(ToolExecution).where(
            *tool_window, ToolExecution.status == "failed")
    ).scalar_one()

    return {
        "date": date_str.strip(),
        "total": int(total),
        "by_status": {str(k): int(v) for k, v in by_status.items()},
        "tool_calls": int(tool_calls),
        "blocked": int(blocked),
        "tool_failed": int(tool_failed),
    }


def get_stats_today(db: Session) -> dict:
    """统计今日处理量（等价于 get_stats(今天)）。"""
    return get_stats(db, _now().strftime("%Y-%m-%d"))


# ======================================================================
# 业务数据 CRUD（orders / inventory / returns）
#
# 大白话：这一组函数是"业务底表"的读写口子。迁库之前，这三个数据集只有
#         CSV 一个来源，查一次订单要把整个文件读进内存再聚合；现在改成
#         按主键/索引查库，几百毫秒变几毫秒。
#
# 为什么都集中在这里：data_source.py 要同时支持"查库"和"降级查 CSV"两条路，
#         把"查库"这段全收敛到 crud 里，data_source 里只剩下"选路"的少量代码。
# ======================================================================

# 迁移脚本批量写入时每批的条数。SQLite 单条 INSERT 都有一次隐性事务开销，
# 攒够一批再提交能把 5000 行导入从"几十秒"压到"一两秒"。
BULK_BATCH_SIZE: int = 500


def _as_dict(row_mapping) -> dict:
    """把 SQLAlchemy Row 转成普通 dict，方便透传给上层当"库存快照"。"""
    return dict(row_mapping)


def _parse_dt(value: Any) -> datetime | None:
    """把 CSV 里的时间字符串尽力解析成 datetime，解析不出来返回 None。

    大白话：原始数据的时间列格式不统一（有的带秒、有的只到天、有的干脆是空的），
    能认出来的就转成标准时间存库，认不出来的就存 NULL —— 宁可这个字段空着，
    也不要让一行坏时间把整批导入中断。

    技术细节：按"从精确到宽松"的顺序尝试几种常见格式；SQLite 的 DateTime
    列在写入时若收到字符串会直接报错，所以这里必须转成 datetime 或 None。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null"}:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
                "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _dt_to_str(value: datetime | None) -> str:
    """datetime -> "%Y-%m-%d %H:%M:%S" 字符串；None 原样返回空串。

    领域模型 Order/Return 的 created_at 是字符串类型（跟原来的 CSV 口径一致），
    从库里取出来时要转回去，避免上层突然拿到 datetime 而导致格式变化。
    """
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


# ----------------------------------------------------------------------
# 迁移写入（供 scripts/migrate_csv_to_db.py 调用）
# ----------------------------------------------------------------------
def replace_orders(db: Session, rows: list[dict]) -> int:
    """整表替换订单数据（先清空再批量插入），返回写入条数。

    大白话：迁移就是"把 CSV 的内容完整搬进库"，因此每次都是"清空重灌"而不是
    "增量 merge" —— 省掉"这条是新的还是旧的"的判断，语义也更直观（幂等）。
    想保留旧数据就别加 --reset 之外的花样，本项目不需要增量同步。

    技术细节：用 `bulk_insert_mappings` 而不是逐条 `db.add()`：
    - 它绕过 ORM 的对象状态跟踪，直接把字典塞进 INSERT，几千行的速度差一个数量级；
    - 代价是**不会触发 ORM 事件/default**，所以调用方传进来的 dict 必须自己
      填好所有 NOT NULL 字段（迁移脚本已保证）。
    """
    db.execute(delete(Order))
    total = 0
    for i in range(0, len(rows), BULK_BATCH_SIZE):
        batch = rows[i:i + BULK_BATCH_SIZE]
        db.bulk_insert_mappings(Order, batch)
        total += len(batch)
    db.commit()
    log.info("持久化：订单表已重灌 %d 条", total)
    return total


def replace_inventory(db: Session, rows: list[dict]) -> int:
    """整表替换库存数据（先清空再批量插入），返回写入条数。"""
    db.execute(delete(Inventory))
    total = 0
    for i in range(0, len(rows), BULK_BATCH_SIZE):
        batch = rows[i:i + BULK_BATCH_SIZE]
        db.bulk_insert_mappings(Inventory, batch)
        total += len(batch)
    db.commit()
    log.info("持久化：库存表已重灌 %d 条", total)
    return total


def replace_returns(db: Session, rows: list[dict]) -> int:
    """整表替换退货数据（先清空再批量插入），返回写入条数。"""
    db.execute(delete(Return))
    total = 0
    for i in range(0, len(rows), BULK_BATCH_SIZE):
        batch = rows[i:i + BULK_BATCH_SIZE]
        db.bulk_insert_mappings(Return, batch)
        total += len(batch)
    db.commit()
    log.info("持久化：退货表已重灌 %d 条", total)
    return total


def count_orders(db: Session) -> int:
    """订单表行数。"""
    return int(db.execute(select(func.count()).select_from(Order)).scalar_one())


def count_inventory(db: Session) -> int:
    """库存表行数。"""
    return int(db.execute(select(func.count()).select_from(Inventory)).scalar_one())


def count_returns(db: Session) -> int:
    """退货表行数。"""
    return int(db.execute(select(func.count()).select_from(Return)).scalar_one())


# ----------------------------------------------------------------------
# 订单查询
# ----------------------------------------------------------------------
def _row_to_order(row: Order) -> DomainOrder:
    """ORM Order 行 -> 领域 Order。items 字段把 JSON 字符串解回 list。"""
    try:
        items = json.loads(row.items) if row.items else []
    except (TypeError, ValueError):
        # items 存的是坏 JSON（正常不该发生）——降级成空明细，
        # 而不是让整个订单查询抛异常，否则一个坏字段会把所有查询带崩。
        log.warning("订单 %s 的 items 字段无法解析，按空明细处理", row.order_id)
        items = []
    if not isinstance(items, list):
        items = []
    return DomainOrder(
        order_id=row.order_id,
        customer=row.customer or "",
        store=row.store or "",
        amount=float(row.amount or 0.0),
        status=row.status or "",
        items=items,
        created_at=_dt_to_str(row.created_at),
    )


def get_order(db: Session, order_id: str) -> DomainOrder | None:
    """按订单号查一笔订单；不存在返回 None。走主键索引，等价于 O(1)。"""
    if not order_id:
        return None
    row = db.get(Order, str(order_id).strip())
    return _row_to_order(row) if row is not None else None


def list_orders(db: Session, limit: int | None = None) -> list[DomainOrder]:
    """取全部订单（可按 limit 截断），按创建时间倒序。

    为什么要有 limit：全量 2432 条订单一次性返回给上层做遍历是可行的，
    但留个上限参数，数据量涨上来时调用方有处可收紧。
    """
    stmt = select(Order).order_by(Order.created_at.desc(), Order.order_id.asc())
    if limit is not None:
        try:
            stmt = stmt.limit(max(0, int(limit)))
        except (TypeError, ValueError):
            pass
    return [_row_to_order(r) for r in db.execute(stmt).scalars().all()]


def search_orders(db: Session, customer: str | None = None,
                  store: str | None = None, limit: int = 100) -> list[DomainOrder]:
    """按客户名 / 门店名模糊查询订单。

    大白话：给"这个客户在我们店买过什么"这类问题用的，客户名和门店名都支持
    部分匹配（`LIKE %xx%`），两个条件同时给就是"且"的关系。
    """
    stmt = select(Order)
    if customer:
        stmt = stmt.where(Order.customer.like(f"%{customer}%"))
    if store:
        stmt = stmt.where(Order.store.like(f"%{store}%"))
    stmt = stmt.order_by(Order.created_at.desc()).limit(max(1, int(limit)))
    return [_row_to_order(r) for r in db.execute(stmt).scalars().all()]


# ----------------------------------------------------------------------
# 库存查询
# ----------------------------------------------------------------------
def get_inventory(db: Session, store: str, sku: str) -> dict | None:
    """按 (门店, SKU) 查库存快照；不存在返回 None。

    返回 dict（不是领域对象）—— 因为库存天然是"一组名值对"，
    上层各处的消费方式都是 `inv["available"]` 这种取值，包一层 dataclass 反而累赘。

    返回结构：
        {"store":..., "sku":..., "qty":..., "available":..., "safety":..., "updated_at":...}
    """
    if not store or not sku:
        return None
    stmt = select(Inventory).where(
        Inventory.store == str(store).strip(),
        Inventory.sku == str(sku).strip(),
    )
    row = db.execute(stmt).scalars().first()
    if row is None:
        return None
    return {
        "store": row.store,
        "sku": row.sku,
        "qty": int(row.qty),
        "available": int(row.available),
        "safety": int(row.safety),
        "updated_at": _dt_to_str(row.updated_at),
    }


def adjust_inventory(db: Session, store: str, sku: str, delta: int) -> int | None:
    """扣减库存（delta 为负数即扣减；正数即回补）。

    大白话：补发/退款落地时要把可用库存减掉。返回扣减后的可用库存，
    找不到该 (门店, SKU) 记录时返回 None（调用方据此判断"扣不了"）。

    技术细节：`available` 和 `qty` 同步调整，且**允许扣成负数**——
    为什么不在库层面卡住负数：真实业务里"超卖"是必须被记录下来的事实，
    拦在 SDK 层会让上层丢数据；是否要因此拒绝补发由安全闸门判断，
    库存层只负责如实反映结果。
    """
    if not store or not sku:
        return None
    stmt = select(Inventory).where(
        Inventory.store == str(store).strip(),
        Inventory.sku == str(sku).strip(),
    )
    row = db.execute(stmt).scalars().first()
    if row is None:
        return None
    step = int(delta)
    row.available = int(row.available) + step
    row.qty = int(row.qty) + step
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return int(row.available)


# ----------------------------------------------------------------------
# 退货查询（迁库新增能力）
# ----------------------------------------------------------------------
def _row_to_return(row: Return) -> DomainReturn:
    """ORM Return 行 -> 领域 Return。"""
    return DomainReturn(
        order_id=row.order_id or "",
        customer=row.customer or "",
        store=row.store or "",
        sku=row.sku or "",
        reason=row.reason or "",
        amount=float(row.amount or 0.0),
        created_at=_dt_to_str(row.created_at),
    )


def get_returns(db: Session, customer: str | None = None, sku: str | None = None,
                days: int | None = 30) -> list[DomainReturn]:
    """查退货记录，按客户 / SKU 过滤，默认只看最近 30 天。

    参数：
        customer: 收货人（精确匹配）。None 表示不限。
        sku:      商品编码（精确匹配）。None 表示不限。
        days:     只取"申请时间在最近 N 天内"的记录。
                  **None 表示不限时间**（重要：迁移进来的演示数据申请时间在
                  2026-09 ~ 2027-12，相对"今天"是未来日期，用 days=30 会一条都
                  查不到 —— 需要看全量时显式传 days=None）。

    返回：
        list[Return]，按申请时间倒序（最近的在前）。

    技术细节：时间过滤用 `>= now - N 天` 的左闭区间（不回看将来数据是否超界，
    "未来时间"的记录本来就会被 >= 命中，符合"最近 N 天"的宽松直觉）。
    """
    stmt = select(Return)
    if customer:
        stmt = stmt.where(Return.customer == str(customer).strip())
    if sku:
        stmt = stmt.where(Return.sku == str(sku).strip())
    if days is not None:
        try:
            n = int(days)
        except (TypeError, ValueError):
            n = 30
        if n > 0:
            stmt = stmt.where(Return.created_at >= _now() - timedelta(days=n))
    stmt = stmt.order_by(Return.created_at.desc(), Return.id.desc())
    return [_row_to_return(r) for r in db.execute(stmt).scalars().all()]


def get_customer_return_rate(db: Session, customer: str, days: int | None = 30) -> float:
    """算某客户的退货率 = 该客户下的订单中被退过的比例。

    大白话："这个客户买的东西里有多少退回来了？"—— 退货率高到一定程度
    就该人工介入，不能放任 Agent 一路自动退款。

    【为什么按订单关联而不是按"退货表的收货人字段"直接数】
    实测数据里退货表的「收货人」是脱敏代号（"收货人80"），订单表里是真实姓名
    （"华洋"），**两个字段没有任何交集**，直接按名字数永远是 0。但退货表的
    「原订单号」100% 能在订单表里找到对应订单（实测匹配率 1.0）。
    所以正确口径是：**以订单为中心** ——
        分母 = 该客户期内的订单数
        分子 = 这些订单里"至少有一笔退货"的订单数
    这样"退货率"才落在 0~1 且真正反映客户行为。

    【为什么分子分母都按订单去重】
    同一个订单可能退多件商品（多行退货记录），按行数算会让退货率超过 1，
    语义上就说不通了。按订单去重后含义清晰："这个客户每 100 单里有几单退过货"。

    参数：
        customer: 收货人（订单表的真实姓名）
        days:     统计窗口天数。None 表示不限时间（全量口径）。

    返回：
        0.0 ~ 1.0 的比值；该客户在窗口内一单都没有时返回 0.0。

    技术细节：用一条 JOIN 查询在库里算，不把订单拉到内存再逐条查退货。
    """
    name = str(customer or "").strip()
    if not name:
        return 0.0

    lower: datetime | None = None
    if days is not None:
        try:
            n = int(days)
        except (TypeError, ValueError):
            n = 30
        if n > 0:
            lower = _now() - timedelta(days=n)

    # 分母：该客户期内的订单数
    order_stmt = select(func.count()).select_from(Order).where(Order.customer == name)
    if lower is not None:
        order_stmt = order_stmt.where(Order.created_at >= lower)
    order_count = int(db.execute(order_stmt).scalar_one())
    if order_count <= 0:
        return 0.0

    # 分子：该客户的订单里"有退货记录"的**去重订单数**。
    # 用 distinct(order_id) 而不是 count(*)：一个订单退多件只算一单。
    return_stmt = (
        select(func.count(func.distinct(Return.order_id)))
        .select_from(Return)
        .join(Order, Order.order_id == Return.order_id)
        .where(Order.customer == name)
    )
    if lower is not None:
        # 时间窗按**退货申请时间**卡（"近 30 天这个客户退货了几单"），
        # 而不是订单创建时间 —— 客户关心的是"最近在退"，不是"最近买的老单被退了"。
        return_stmt = return_stmt.where(Return.created_at >= lower)
    returned_orders = int(db.execute(return_stmt).scalar_one())

    # 分子理论上不会超过分母（都从同一批订单里取），但防御性地夹一下，
    # 避免将来数据不一致时返回一个 >1 的"率"把上层阈值判断搞乱。
    return round(min(returned_orders, order_count) / order_count, 4)
