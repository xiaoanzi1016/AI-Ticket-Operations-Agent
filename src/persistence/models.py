# -*- coding: utf-8 -*-
"""持久化层：ORM 数据模型。

【这个文件是干什么的】
项目原来的记忆层（src/memory/store.py）全部活在内存里，程序一关就清零。
这个文件用 SQLAlchemy 的"声明式模型"把数据搬到 SQLite 文件中，让 Agent 有
"长期记忆"。一共两族表：

一、运行台账（Phase 1 引入）—— 记录 Agent 自己干了什么：
- Task          —— 一次任务（= 一次 `agent.run()`）的台账：谁提的、什么状态、结果。
- ToolExecution —— 这一次任务里每一次工具调用 / 安全闸门判定的流水：调了什么、
                   传了什么参数、拿到什么结果、成功还是被拦。
  两张表是一对多：一条 Task 对应 N 条 ToolExecution。有了它就能回答
  "这个任务当时到底发生了什么" —— 也就是审计追溯。

二、业务数据（迁库引入）—— Agent 查询所依赖的"业务底表"：
- Order     —— 订单主表（一单一行，一单多 SKU 的明细塞进 items JSON 字段）。
- Inventory —— 库存表，按 (门店, SKU) 唯一。
- Return    —— 退货表，一行 = 一笔售后申请，关联原订单号。
  原先这三份数据分别躺在 data/mock/ 下的三个 CSV 里，每次进程启动都要重新
  读文件 + 清洗 + 聚合；迁到 SQLite 后既省掉启动开销，也终于能用 SQL 做
  联表统计（比如"某客户近 30 天退货率"这种原来根本算不出来的问题）。
  注意：CSV 仍然保留，数据库缺失时数据层会自动降级回 CSV（见 src/data_source.py）。

【技术细节】
- SQLAlchemy 2.0 的 `Mapped[...]` + `mapped_column(...)` 声明式写法，
  类型注解即列类型来源，比 1.x 的 `Column()` 更直观且能被 mypy 静态检查。
- `from __future__ import annotations` 让注解在运行时是字符串，SQLAlchemy
  会在映射阶段解析，因此可以放心写 `str | None` 这种 3.10+ 的联合类型。
- 时间列统一用"本地时间 + 无时区"存（与项目现有日志 `%Y-%m-%d %H:%M:%S` 口径一致），
  避免 SQLite 存时区串导致 CLI 按日期查询时对不上。
- 业务表的"时间"来自 CSV 里已有的字符串（如"订单创建时间"），可能为空/脏；
  因此这几列一律 `nullable=True`，由迁移脚本尽力解析，解析不出来就存 NULL，
  不让一行脏时间把整批导入搞崩。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类。

    大白话：这是两张表的"户口本"，所有表都挂在这上面。
    技术细节：`DeclarativeBase` 子类持有 `metadata`，`metadata.create_all(engine)`
             一次性把本模块里声明过的所有表建出来（见 database.init_db）。
    """


class Task(Base):
    """任务表：一次任务处理（一次 agent.run 调用）的完整台账。

    大白话：每次用户提一个诉求，就开一张"工单小票"，记下谁提的、处理到哪一步了。
    """

    __tablename__ = "tasks"

    # ---- 主键 ----------------------------------------------------------
    # 自增整数，只给数据库内部用（对外一律用下面可读的 task_id）
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ---- 业务标识 ------------------------------------------------------
    # 任务编号，形如 "task_3f9a1c07be24d5e1"（uuid4 去掉横线后取前 16 位）。
    # 为什么不用裸 uuid4：日志/CLI 里肉眼可读、便于口头传递；
    # 长度 21 远小于列宽 36，且 16 位十六进制 = 64 bit，碰撞概率可忽略。
    task_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)

    # ---- 业务内容 ------------------------------------------------------
    # 用户原始输入（不做任何改写，审计要的是原文）
    user_input: Mapped[str] = mapped_column(Text, nullable=False)

    # 任务状态：processing（处理中）/ completed（已完成）/ failed（异常失败）
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")

    # 最终结果摘要：JSON 字符串（回答、建议、闸门判定、本次 trace 指标）。
    # 可空：任务还在处理中时没有摘要；异常失败时可能只有 error。
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- 时间 ----------------------------------------------------------
    # 创建时间。default=datetime.now（传函数不传调用结果），
    # 这样每条 INSERT 各自取当时的时间，而不是模块导入时被"冻结"的同一个值。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, index=True
    )
    # 更新时间：onupdate 让每次 UPDATE 自动刷新，不用调用方记得手动维护
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    # ---- 关联 ----------------------------------------------------------
    # 一个任务的所有工具执行流水。删任务时顺带删流水（没有孤儿记录）。
    executions: Mapped[list["ToolExecution"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="ToolExecution.id",
        primaryjoin="Task.task_id == ToolExecution.task_id",
        foreign_keys="ToolExecution.task_id",
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (f"<Task task_id={self.task_id!r} status={self.status!r} "
                f"created_at={self.created_at:%Y-%m-%d %H:%M:%S}>"
                if self.created_at else f"<Task task_id={self.task_id!r} status={self.status!r}>")


class ToolExecution(Base):
    """工具执行记录表：一次任务里每一次工具调用 / 闸门判定的流水。

    大白话：Agent 每动一次手（查订单、过安全闸门…），就在这条任务的账本上记一行。
    注意这里记的 **不只是成功**：失败、被安全闸门拦截（blocked）同样要落库 ——
    "被拦下来了" 恰恰是审计最关心的部分。
    """

    __tablename__ = "tool_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 所属任务编号。这里外键指向的是 tasks.task_id（业务唯一键），不是自增主键，
    # 这样查流水时可以直接拿用户手上那个 "task_xxx" 去关联，不需要回表换算。
    # SQLite 默认不强制外键，database.py 里用 PRAGMA foreign_keys=ON 打开了。
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.task_id"), index=True, nullable=False
    )

    # 工具/环节名称。真实工具名如 "query_order"；
    # 安全闸门判定统一写成 "safety_gate"（具体动作类型在 input_params.action_type 里）。
    tool_name: Mapped[str] = mapped_column(String(50), nullable=False)

    # 输入参数：JSON 字符串（dict 序列化后）。闸门记录里会带 action_type。
    input_params: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # 输出结果：JSON 字符串，可空（例如工具抛异常时没有任何输出）。
    # 超过 10000 字符会被 crud._dumps 截断并追加「结果已截断」标记，防止单行把库撑爆。
    output_result: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 执行状态：success（成功）/ failed（执行失败）/ blocked（被安全闸门拦截）
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    # 失败/被拦截的原因说明（可空）
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 执行时间（流水时间轴，CLI 按时间正序展示执行链路）
    executed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, index=True
    )

    # 反向关联到所属任务
    task: Mapped["Task"] = relationship(
        back_populates="executions",
        primaryjoin="Task.task_id == ToolExecution.task_id",
        foreign_keys="ToolExecution.task_id",
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (f"<ToolExecution task_id={self.task_id!r} tool={self.tool_name!r} "
                f"status={self.status!r}>")


# ======================================================================
# 以下为「业务数据表」：从 data/mock/*.csv 迁入，供 Agent 查询使用。
# 与上面的运行台账共用同一个 SQLite 库（data/agent_operations.db），
# 但语义完全独立 —— 台账记"Agent 做了什么"，业务表是"业务事实长什么样"。
# ======================================================================


class Order(Base):
    """订单主表：一行 = 一个订单号（一单多 SKU 已聚合成 items）。

    大白话：原来 orders.csv 是一行一个商品明细，同一个订单号会出现好几行；
    这里按"一个订单一行"重新组织，明细压缩进 `items` JSON 字段，金额是合计值。
    这样 `get_order("PO20260928-00001")` 一次主键查询就能拿到完整订单，不用扫全表。
    """

    __tablename__ = "orders"

    # 主键直接用业务订单号 —— 它本身唯一且可读，
    # 比再加一个自增 id 更省事，查询计划也更直接（不需要额外唯一索引）。
    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # 收货人（对应 CSV 的「收货人」列）。脏数据里可能为空，故可空。
    customer: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 门店名（对应 CSV 的「门店」列）
    store: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 订单合计金额 = Σ(数量 × 单价)，单位元，保留两位小数。
    # 用 Float 而非 Numeric：本项目只做展示与阈值比较，不做财务级精确累加，
    # 且 SQLite 的 Numeric 实际仍是浮点存储，用 Float 语义更诚实。
    amount: Mapped[float] = mapped_column(nullable=False, default=0.0)

    # 订单状态（对应 CSV 的「订单状态」），如"已签收"/"待发货"。
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 下单时间。CSV 里是字符串（"2026-09-28 10:23:11" 之类），
    # 迁移时解析为 datetime；解析失败则存 NULL。
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # 一单多 SKU 的明细，JSON 数组字符串：
    #   [{"sku": "SKU001", "name": "xx", "spec": "xx", "qty": 2, "price": 39.9}, ...]
    # 为什么用 Text 存 JSON 而不是再拆一张 order_items 表：
    # 明细只跟着订单整体一起读写，从不单独按明细查询，拆表只会徒增 join 成本；
    # SQLite 从 3.38 起支持 JSON 函数，真需要时也能在 SQL 里查。
    items: Mapped[str | None] = mapped_column(Text, nullable=True, default="[]")

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (f"<Order order_id={self.order_id!r} customer={self.customer!r} "
                f"amount={self.amount!r} status={self.status!r}>")


class Inventory(Base):
    """库存表：一行 = 一个 (门店, SKU) 的库存快照。

    大白话：回答"某个店某个商品还剩多少、还能卖多少（可用）、安全线是多少"。
    原来 inventory.csv 里同店同 SKU 可能有多个仓库行，迁移时已按 (store, sku) 累加。
    """

    __tablename__ = "inventory"

    # 主键用自增整数：业务上唯一键是 (store, sku)，但让它当联合主键会在
    # 关联/更新时处处要传两个字段，不如给个代理键 + 联合唯一索引。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    store: Mapped[str] = mapped_column(String(128), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)

    # 库存数量（总库存）
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 可用库存：真正能被下单占用的量（总库存 - 已锁定等）
    available: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 安全库存：低于这条线就该补货了，是安全闸门判断"要不要拦补发"的依据之一
    safety: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 快照更新时间：每次库存变动（扣减）时刷新，用于判断数据新鲜度
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    # 联合唯一索引：保证一个 (门店, SKU) 只有一行，
    # 迁移脚本的 upsert 与执行层的扣减都依赖这条约束来定位唯一行。
    __table_args__ = (
        Index("ix_inventory_store_sku", "store", "sku", unique=True),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (f"<Inventory store={self.store!r} sku={self.sku!r} "
                f"qty={self.qty!r} available={self.available!r}>")


class Return(Base):
    """退货表：一行 = 一笔售后/退货申请。

    大白话：某个客户针对某个原订单的某个商品发起了退货，记下原因和金额。
    这是原来三个 CSV 里唯一"有数据但没入口"的一份 —— 迁库后新增的
    `get_returns` / `get_customer_return_rate` 才让它第一次能被 Agent 查询。

    技术细节：本表用自增 id 做主键而非 order_id —— 一个原订单可能有多笔退货
    （不同 SKU 分别退、或退一次后又退一次），order_id 在这里不唯一。
    """

    __tablename__ = "returns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 原订单号（对应 CSV 的「原订单号」）。故意不建外键约束：
    # 退货数据里存在原订单已被清理/从未导入的情况，加外键会让迁移直接失败，
    # 而"退货单指向一个查不到的订单"本身也是有价值的数据质量问题信号。
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    customer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    store: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sku: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 退货原因（对应 CSV 的「退货原因」/「售后原因」列）
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 退款金额
    amount: Mapped[float] = mapped_column(nullable=False, default=0.0)

    # 申请时间（对应 CSV 的「申请时间」列，注意列名不叫"退货时间"）
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (f"<Return id={self.id!r} order_id={self.order_id!r} "
                f"customer={self.customer!r} sku={self.sku!r} amount={self.amount!r}>")
