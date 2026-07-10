# Phase 1 持久化层交付说明

> 目标：给 Agent 装「长期记忆」——所有任务执行记录自动落 SQLite，支持历史查询与审计追溯。

---

## 0. 先对齐三处事实偏差（请先读）

需求书描述的项目结构与本仓库**真实结构不一致**。我按真实代码落地，并把需求里的概念做了等价映射。

**偏差 1｜目录名与业务工具不同**

| 需求书里的写法 | 本仓库的真实情况 |
|---|---|
| 项目根目录 `AI-Order-Operations-Agent` | 实际为 `AI-Ticket-Operations-Agent` |
| 工具 `data_quality_check` / `order_audit` / `generate_anomaly_report` / `generate_delivery_order` | 实际为 `query_order` / `query_logistics` / `check_inventory` / `create_suggestion`（注册给 LLM 的只有前 3 个只读工具） |
| `src/tests/test_order_workflow.py` | 实际为 `tests/` 下 11 个测试文件、共 71 项用例 |

**偏差 2｜`src/models.py` 会与现有包冲突，改放 `src/persistence/`**

本仓库 `src/models/` 已经是**大模型接入层**包（`LLMClient` 等）。若在 `src/` 下再放一个同名的
`models.py`，Python 的包解析会让其中一个失效。因此新建 `src/persistence/` 包（与既有的
domain / tools / safety / execution / memory 等层并列）来承载 ORM 模型：

- 需求书的 `src/database.py`  → `src/persistence/database.py`
- 需求书的 `src/models.py`    → `src/persistence/models.py`（**这一条是必须改的**）
- 需求书的 `src/crud.py`      → `src/persistence/crud.py`
- 需求书的 `src/query_cli.py` → `src/query_cli.py`（无冲突，按原路径交付）

**偏差 3｜SQLAlchemy 版本**

需求锁定 `sqlalchemy==2.0.36`。但本仓库 venv 是 **Python 3.14.7**，实测 2.0.36 解析
`Mapped[str | None]` 时抛 `TypeError: descriptor '__getitem__' requires a 'typing.Union'
object but received a 'tuple'`（Python 3.14 的 typing 内部 API 变更所致）。
已改为 `sqlalchemy>=2.0.36,<2.1.0`，实测 **2.0.54** 通过全部用例。
若运行环境固定 Python 3.11，原定的 2.0.36 同样可用。

**概念等价映射（需求 → 本仓库）**

| 需求书的说法 | 本仓库对应物 |
|---|---|
| Task（一次任务） | 一次 `TicketAgent.run()` 调用 |
| `generate_delivery_order` 被拦截 → `status="blocked"` | `reissue` / `refund` 等敏感动作被**安全闸门拒绝**，或被**库存闸门**额外拦下 → `status="blocked"` |
| 「工具执行记录」 | ① `_dispatch_tool` 里的真实工具调用；② `safety_gate` 环节的闸门判定（两者都写进 `tool_executions` 表） |

---

## 1. 交付物清单

**新增**

| 文件 | 说明 |
|---|---|
| `src/persistence/__init__.py` | 包出口 |
| `src/persistence/database.py` | 引擎 / 会话工厂 / `get_db()` 上下文管理器 / 建表 |
| `src/persistence/models.py` | ORM 模型：`Task`、`ToolExecution` |
| `src/persistence/crud.py` | 全部数据库读写函数 |
| `src/query_cli.py` | 历史查询命令行工具 |
| `tests/conftest.py` | 把测试期数据库重定向到临时目录 |
| `tests/test_persistence.py` | 持久化层 17 项单测 |

**修改**

| 文件 | 改动 |
|---|---|
| `src/agent.py` | 任务创建、工具流水、闸门判定、终态回写（见第 8 节） |
| `requirements.txt` | 新增 sqlalchemy |
| `.gitignore` | 忽略 `data/agent_operations.db` 等 |
| `README.md` | 增补持久化层说明 |

---

## 2. `requirements.txt`

```text
# 依赖
openai>=1.0.0
pandas>=2.0.0
openpyxl>=3.1.0
pydantic>=2.0.0
python-dotenv>=1.0.0

# 持久化层（Phase 1）：任务/工具执行记录落 SQLite
# 说明：需求原定锁定 2.0.36。实测在 Python 3.14 下 2.0.36 无法解析
#       `Mapped[str | None]` 注解（typing 内部 API 变更，抛 TypeError），
#       需 >= 2.0.44 才兼容 3.14。若运行环境固定 Python 3.11，2.0.36 同样可用。
sqlalchemy>=2.0.36,<2.1.0

# 可选：评测/报告
matplotlib>=3.8.0
```

---

## 3. `.gitignore`

```text
# Python
__pycache__/
*.py[cod]
*.pyo
*.egg-info/

# 虚拟环境
.venv/
venv/
env/

# 环境变量
.env
.env.*

# 日志
logs/
*.log

# 数据生成产物（保留 mock 源数据）
data/*.xlsx
data/*.csv
!data/mock/

# 持久化数据库（Phase 1）：本地运行产生的审计留痕，不入版本库
data/agent_operations.db
data/*.db
data/*.db-journal
data/*.db-wal
data/*.db-shm

# 测试/工具缓存
.pytest_cache/
.ruff_cache/
.mypy_cache/

# 运行产物
outputs/
*.tmp
*.temp

# IDE
.vscode/
.idea/

# OS
.DS_Store
Thumbs.db
```

---

## 4. `src/persistence/database.py`

```python
# -*- coding: utf-8 -*-
"""持久化层：数据库引擎与会话管理。

【这个文件是干什么的】
整个项目只在这里决定"数据存到哪个文件、怎么连、怎么开会话"。上层（agent.py、
query_cli.py）不关心连接细节，只管跟我要一个会话来用。

【技术细节】
- 数据库选 SQLite：单文件、零配置、不用起服务，非常适合"本地留痕/审计"这个场景。
  文件固定落在 `data/agent_operations.db`（路径来自项目既有配置 settings.data_dir，
  不新增配置项，避免动 config.py）。
- SQLAlchemy 2.0 的 `sessionmaker` 生产会话；`get_db()` 用 `@contextmanager`
  把「开 → 用 → 提交/回滚 → 一定关闭」这套模板代码收进一处，
  调用方写 `with get_db() as db:` 就不会忘记 close（漏 close 会一直占着连接）。
- 用 `URL.create(...)` 拼连接串而不是手写 f-string：项目路径含中文（D:\\工作区\\...），
  手工拼串容易被 SQLAlchemy 当成 URL 结构去解析，URL.create 会正确转义。
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import settings
from src.logger import log
from src.persistence.models import Base

# ----------------------------------------------------------------------
# 1) 库文件位置
# ----------------------------------------------------------------------
# 大白话：账本就放在项目的 data/ 目录下，名字叫 agent_operations.db。
# AGENT_DB_PATH 环境变量可覆盖它 —— 主要给测试用：跑 pytest 时指向临时文件，
# 避免测试数据把真实的审计库写脏（见 tests/conftest.py）。
_DB_PATH_OVERRIDE = os.getenv("AGENT_DB_PATH", "").strip()
DB_PATH: Path = (Path(_DB_PATH_OVERRIDE) if _DB_PATH_OVERRIDE
                 else settings.data_dir / "agent_operations.db")


def build_engine(path: Path | str) -> Engine:
    """按给定文件路径创建 SQLite 引擎。

    大白话：这个函数只负责"把某个 db 文件包成一个可用的连接器"。

    为什么要单独暴露出来：测试要指向临时文件（`tmp_path/xxx.db`）或内存库
    （`:memory:`），绝不能污染真实的 `data/agent_operations.db`。
    生产代码则用模块底部的全局 `engine`。

    技术细节：用 `URL.create(...)` 拼连接串而不是手写 f-string —— 项目路径含
    中文（D:\\工作区\\...），手工拼串容易被 SQLAlchemy 当 URL 结构去解析，
    URL.create 会正确转义。
    """
    url = URL.create("sqlite", database=str(path))
    eng = create_engine(url, echo=False, future=True)

    # SQLite 出于历史兼容，默认**不强制**外键约束。
    # 这里每建一条底层连接就打开 PRAGMA foreign_keys，保证 tool_executions.task_id
    # 不能指向一个不存在的任务（审计库最怕脏关联）。
    @event.listens_for(eng, "connect")
    def _enable_sqlite_fk(dbapi_conn, _record):  # pragma: no cover - 驱动回调
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return eng


def make_session_factory(target_engine: Engine):
    """按给定引擎造一个会话工厂（与全局 SessionLocal 同配置）。

    大白话：会话工厂就是"会话的模具"，每次调用它就拿到一个全新会话。

    技术细节：`expire_on_commit=False` 让 commit 后对象属性不立刻失效，
    这样 crud 里 commit 完还能直接读 `task.task_id` 返回给调用方，不用回查一次。
    """
    return sessionmaker(bind=target_engine, autoflush=False, expire_on_commit=False)


# 全局引擎 + 会话工厂（单进程演示用；多进程/多租户请改用注入式实例）
settings.data_dir.mkdir(parents=True, exist_ok=True)
engine: Engine = build_engine(DB_PATH)
SessionLocal = make_session_factory(engine)


# ----------------------------------------------------------------------
# 2) 建表
# ----------------------------------------------------------------------
def init_db(target_engine: Engine | None = None) -> None:
    """建表（幂等：表已存在就什么都不做）。

    大白话：确保账本和它的两页表格都在，第一次运行自动建好，之后跑多少次都安全。
    技术细节：`Base.metadata.create_all` 会对 metadata 里登记过的每张表发
             `CREATE TABLE IF NOT EXISTS`，所以可以无脑在启动时调用。
    """
    eng = target_engine or engine
    Base.metadata.create_all(bind=eng)
    log.debug("持久化层：数据表已就绪 (%s)", eng.url.database)


# ----------------------------------------------------------------------
# 3) 会话上下文管理器
# ----------------------------------------------------------------------
@contextmanager
def get_db() -> Iterator[Session]:
    """数据库会话上下文管理器 —— 上层拿会话的唯一入口。

    大白话：`with get_db() as db:` 进去时给你一个会话，出来时自动"提交或回滚"
    并把连接还回去。你在里面忘了关也不会泄漏连接。

    技术细节：
    - 正常退出  -> commit()（把这次要写的东西落盘）
    - 抛异常    -> rollback()（半截的写入不留在库里）后重新抛出，不吞异常
    - 无论如何  -> finally 里 close()，把连接交还连接池

    用法：
        with get_db() as db:
            task = create_task(db, "客户要求补发")
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# 导入本模块即建表：保证"运行即用、零配置"，不需要用户手动执行迁移脚本。
init_db()
```

---

## 5. `src/persistence/models.py`

```python
# -*- coding: utf-8 -*-
"""持久化层：ORM 数据模型（Task / ToolExecution）。

【这个文件是干什么的】
项目原来的记忆层（src/memory/store.py）全部活在内存里，程序一关就清零。
这个文件用 SQLAlchemy 的"声明式模型"把两张表搬到 SQLite 文件中，让 Agent 有
"长期记忆"：

- Task          —— 一次任务（= 一次 `agent.run()`）的台账：谁提的、什么状态、结果。
- ToolExecution —— 这一次任务里每一次工具调用 / 安全闸门判定的流水：调了什么、
                   传了什么参数、拿到什么结果、成功还是被拦。

两张表是一对多：一条 Task 对应 N 条 ToolExecution。有了它就能回答
"这个任务当时到底发生了什么" —— 也就是审计追溯。

【技术细节】
- SQLAlchemy 2.0 的 `Mapped[...]` + `mapped_column(...)` 声明式写法，
  类型注解即列类型来源，比 1.x 的 `Column()` 更直观且能被 mypy 静态检查。
- `from __future__ import annotations` 让注解在运行时是字符串，SQLAlchemy
  会在映射阶段解析，因此可以放心写 `str | None` 这种 3.10+ 的联合类型。
- 时间列统一用"本地时间 + 无时区"存（与项目现有日志 `%Y-%m-%d %H:%M:%S` 口径一致），
  避免 SQLite 存时区串导致 CLI 按日期查询时对不上。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
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
```

---

## 6. `src/persistence/crud.py`

```python
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

【约定】
- 所有函数自带 `db.commit()`：调用方不用记得提交，拿到的对象已经是持久化状态。
- 所有函数只抛 SQLAlchemy 原生异常，**不吞异常**；"失败不阻断主流程"由上层
  （agent.py）用 try-except 兜底 —— 库挂了也不该让工单处理崩掉。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.logger import log
from src.persistence.models import Task, ToolExecution

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
```

---

## 7. `src/query_cli.py`

```python
# -*- coding: utf-8 -*-
"""命令行查询工具：翻 Agent 的"历史台账"。

【这个文件是干什么的】
Phase 1 把任务和执行流水落进了 SQLite，但数据库文件人不好直接看。
这个 CLI 就是账本的"查询窗口"，一条命令就能回答：
- 最近处理了哪些任务？          python src/query_cli.py recent 10
- 某个任务当时到底干了什么？    python src/query_cli.py task task_3f9a1c07be24d5e1
- 某一天处理了哪些任务？        python src/query_cli.py date 2026-09-30
- 今天处理了多少、拦了多少？    python src/query_cli.py stats today

【技术细节】
- `argparse` 子命令：每个子命令一个独立 handler，新增查询方式不影响现有命令。
- 表格自己画（不引 prettytable）：项目 requirements 里没有这个依赖，
  为了一个 CLI 多加一个第三方包不划算。
- 中文对齐：中日韩字符在等宽字体里占 2 个字符宽，直接 `str.ljust()` 会错位。
  这里用 `unicodedata.east_asian_width` 计算"显示宽度"再补空格。
- Windows 控制台默认可能是 GBK，打印中文会 UnicodeEncodeError；
  因此在入口处把 stdout 重设为 UTF-8（失败也不致命，静默跳过）。
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

# 让 `python src/query_cli.py` 也能 import src（脚本在 src/ 下时 sys.path 只有 src/）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.persistence import crud  # noqa: E402
from src.persistence.database import DB_PATH, get_db  # noqa: E402

# 状态 -> 中文展示（库里存英文枚举，展示给人看中文）
_STATUS_ZH = {
    "processing": "处理中",
    "completed": "已完成",
    "failed": "失败",
    "success": "成功",
    "blocked": "已拦截",
}


# ----------------------------------------------------------------------
# 显示宽度 / 表格
# ----------------------------------------------------------------------
def _disp_width(text: str) -> int:
    """计算字符串在等宽终端里的显示宽度（中文按 2 列算）。"""
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _clip(text: str, limit: int) -> str:
    """按显示宽度截断，超出补省略号。"""
    text = str(text).replace("\n", " ").replace("\r", " ")
    if _disp_width(text) <= limit:
        return text
    out, width = "", 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if width + w > limit - 1:
            break
        out += ch
        width += w
    return out + "…"


def _pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补空格对齐。"""
    gap = width - _disp_width(text)
    if gap <= 0:
        return str(text)
    return (" " * gap + str(text)) if align == "right" else (str(text) + " " * gap)


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    """画一张 ASCII 表格（按显示宽度对齐，中文不会错位）。"""
    cols = len(headers)
    normalized = [[_clip(c, 60) for c in (list(r) + [""] * cols)[:cols]] for r in rows]
    widths = [
        max([_disp_width(headers[i])] + [_disp_width(r[i]) for r in normalized])
        for i in range(cols)
    ]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [line, "| " + " | ".join(_pad(headers[i], widths[i]) for i in range(cols)) + " |", line]
    for r in normalized:
        out.append("| " + " | ".join(_pad(r[i], widths[i]) for i in range(cols)) + " |")
    out.append(line)
    return "\n".join(out)


def _status_zh(status: str | None) -> str:
    s = str(status or "")
    return f"{_STATUS_ZH.get(s, s)}({s})" if s in _STATUS_ZH else s


def _fmt_dt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "-"


def _one_line(text: str, limit: int = 40) -> str:
    return _clip(str(text or "").replace("\n", " "), limit)


# ----------------------------------------------------------------------
# 子命令实现
# ----------------------------------------------------------------------
def cmd_recent(args: argparse.Namespace) -> int:
    """列出最近 N 条任务。"""
    with get_db() as db:
        tasks = crud.get_recent_tasks(db, args.limit)
    print(f"最近 {args.limit} 条任务（数据库：{DB_PATH}）")
    if not tasks:
        print("（暂无记录。先跑一次 python scripts/run.py 生成数据）")
        return 0
    print(render_table(
        ["任务ID", "状态", "创建时间", "更新时间", "用户输入"],
        [[t.task_id, _status_zh(t.status), _fmt_dt(t.created_at),
          _fmt_dt(t.updated_at), _one_line(t.user_input)] for t in tasks],
    ))
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    """查看某任务详情 + 完整执行链路。"""
    task_id = args.task_id.strip()
    with get_db() as db:
        task = crud.get_task_by_id(db, task_id)
        if task is None:
            print(f"未找到任务：{task_id}")
            print("提示：用 `python src/query_cli.py recent 10` 查看最近的任务编号。")
            return 1
        execs = crud.get_tool_executions_by_task(db, task_id)

    print("=" * 72)
    print(f"任务详情  {task.task_id}")
    print("=" * 72)
    print(f"状态      : {_status_zh(task.status)}")
    print(f"创建时间  : {_fmt_dt(task.created_at)}")
    print(f"更新时间  : {_fmt_dt(task.updated_at)}")
    print(f"用户输入  : {task.user_input}")

    print("\n--- 结果摘要 ---")
    if task.result_summary:
        try:
            print(json.dumps(json.loads(task.result_summary), ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            # 摘要超长被截断时不再是合法 JSON，原样展示即可
            print(task.result_summary)
    else:
        print("（无，任务可能仍在处理中）")

    print(f"\n--- 执行链路（共 {len(execs)} 条） ---")
    if not execs:
        print("（本次任务未产生工具调用记录）")
        return 0
    print(render_table(
        ["#", "执行时间", "工具/环节", "状态", "输入参数", "结果/错误"],
        [[str(i), _fmt_dt(e.executed_at), e.tool_name, _status_zh(e.status),
          _one_line(e.input_params, 34),
          _one_line(e.error_message or e.output_result or "", 44)]
         for i, e in enumerate(execs, start=1)],
    ))
    return 0


def cmd_date(args: argparse.Namespace) -> int:
    """按日期查询任务。"""
    try:
        with get_db() as db:
            tasks = crud.get_tasks_by_date(db, args.date)
    except ValueError as e:
        print(f"参数错误：{e}")
        return 1
    print(f"{args.date} 的任务（共 {len(tasks)} 条）")
    if not tasks:
        print("（该日期没有记录）")
        return 0
    print(render_table(
        ["任务ID", "状态", "创建时间", "用户输入"],
        [[t.task_id, _status_zh(t.status), _fmt_dt(t.created_at), _one_line(t.user_input)]
         for t in tasks],
    ))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """统计某日处理量（`today` 表示今天）。"""
    target = args.date
    try:
        with get_db() as db:
            data = crud.get_stats_today(db) if target == "today" else crud.get_stats(db, target)
    except ValueError as e:
        print(f"参数错误：{e}")
        return 1

    print("=" * 52)
    print(f"处理量统计  {data['date']}" + ("（今天）" if target == "today" else ""))
    print("=" * 52)
    print(f"任务总数      : {data['total']}")
    by_status = data.get("by_status") or {}
    if by_status:
        for status, count in sorted(by_status.items()):
            print(f"  - {_status_zh(status):<12}: {count}")
    else:
        print("  - （无任务）")
    print(f"工具执行次数  : {data['tool_calls']}")
    print(f"安全闸门拦截  : {data['blocked']}")
    print(f"工具执行失败  : {data['tool_failed']}")
    return 0


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="query_cli.py",
        description="AI-Ticket-Operations-Agent 历史记录查询工具（Phase 1 持久化层）",
        epilog=(
            "示例：\n"
            "  python src/query_cli.py recent 10\n"
            "  python src/query_cli.py task task_3f9a1c07be24d5e1\n"
            "  python src/query_cli.py date 2026-09-30\n"
            "  python src/query_cli.py stats today\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="{recent,task,date,stats}")

    p_recent = sub.add_parser("recent", help="查看最近 N 条任务（默认 10）")
    p_recent.add_argument("limit", nargs="?", type=int, default=10, help="返回条数，默认 10")
    p_recent.set_defaults(func=cmd_recent)

    p_task = sub.add_parser("task", help="查看某任务详情（含全部工具执行记录）")
    p_task.add_argument("task_id", help="任务编号，如 task_3f9a1c07be24d5e1")
    p_task.set_defaults(func=cmd_task)

    p_date = sub.add_parser("date", help="按日期查询任务")
    p_date.add_argument("date", help="日期，格式 YYYY-MM-DD")
    p_date.set_defaults(func=cmd_date)

    p_stats = sub.add_parser("stats", help="统计某日处理量")
    p_stats.add_argument("date", nargs="?", default="today",
                         help="'today' 或 YYYY-MM-DD，默认 today")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台可能是 GBK，中文会编码失败；能改就改成 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):  # pragma: no cover - 非标准输出流
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
```

---

## 8. `src/agent.py`（只展示改动部分，均以 `# PHASE 1` 标注）


### 8.1 ① 脚本直跑引导（stdlib import 之后、src 导入之前）

让 `python src/agent.py` 能直接启动；正常 `import src.agent` 时 `__package__ == "src"`，该分支不执行。

```python
# 允许直接以脚本方式运行本文件（`python src/agent.py`）。
# 这种情形下本模块不属于 src 包，`from src.xxx import ...` 会报 No module named 'src'。
# 只在"被当作脚本直接执行"时把项目根加入 sys.path；
# 正常 `import src.agent` / pytest 时 __package__ == "src"，这段完全不执行。
if __package__ in (None, ""):  # pragma: no cover - 仅脚本直跑场景
    import sys as _sys
    from pathlib import Path as _Path

    _PROJECT_ROOT = _Path(__file__).resolve().parent.parent
    if str(_PROJECT_ROOT) not in _sys.path:
        _sys.path.insert(0, str(_PROJECT_ROOT))
```

### 8.2 ② 持久化导入 + 降级开关

导入失败（未装 sqlalchemy）时降级为「不落库」，原有功能不受影响。

```python
# PHASE 1: 持久化层（SQLite + SQLAlchemy）
# 大白话：给 Agent 装一本"流水账"。每次收到诉求开一张任务小票，每次调工具记一笔，
#         进程退出后记录还在 —— 之后可以用 `python src/query_cli.py` 翻账。
# 技术细节：导入失败（例如环境没装 sqlalchemy）时降级为"不落库"，
#         原有功能完全不受影响。持久化是增强项，不该成为 Agent 能跑起来的前提。
# ----------------------------------------------------------------------
try:
    from src.persistence.crud import create_task as _db_create_task
    from src.persistence.crud import create_tool_execution as _db_create_tool_execution
    from src.persistence.crud import update_task_status as _db_update_task_status
    from src.persistence.database import get_db as _db_session

    PERSISTENCE_AVAILABLE = True
    _PERSISTENCE_IMPORT_ERROR = ""
except Exception as _persist_exc:  # pragma: no cover - 未安装 sqlalchemy 的降级分支
    PERSISTENCE_AVAILABLE = False
    _PERSISTENCE_IMPORT_ERROR = str(_persist_exc)
```

### 8.3 ③ `__init__` 新增 persistence 开关

默认开启；单测想保持库干净可传 `persistence=False`。

```python
                 persistence: bool = True) -> None:
        """
        mock: 是否强制 mock 模式（不调 LLM，规则+数据源演示）。
        use_llm: 是否用真实 LLM 生成处理建议（需要 .env 配置 key）。
                 与 mock 互斥；两者都未设置时，有 key 走真实 LLM、无 key 自动 mock。
        session_store / safety_gate / tracer / tool_registry:
                 可注入依赖（多租户或测试时用独立实例，避免全局单例互相污染）。
        persistence: PHASE 1 新增。是否把本次运行的任务与工具调用落库（默认开）。
                     单元测试想保持 data/agent_operations.db 干净时可传 False；
                     环境未安装 sqlalchemy 时自动降级为 False。
        """
        self.llm = llm or LLMClient()
        has_key = settings.has_model_key
        self.use_llm = use_llm or (has_key and not mock)
        self.mock = mock or not has_key
        self.sessions = session_store or default_session_store
        self.gate = safety_gate or default_safety_gate
        self.tracer = tracer or default_tracer
        self.registry = tool_registry or registry
        # PHASE 1: 持久化开关。缺依赖时自动降级，并明确告警（不静默失效）。
        self.persist = bool(persistence) and PERSISTENCE_AVAILABLE
        if persistence and not PERSISTENCE_AVAILABLE:
            log.warning("持久化不可用，本次运行不落库: %s", _PERSISTENCE_IMPORT_ERROR)
```

### 8.4 ④ `run()` 拆分：外层负责落库，内层 `_run_core` 保持原逻辑不变

异常也必须落一条 failed（异常仍原样抛出，不吞）。

```python
    def run(self, user_input: str, customer: str = "访客",
            session_id: Optional[str] = None) -> dict:
        """处理一条工单 / 用户诉求，返回结构化结果（并留痕落库）。

        大白话：这是对外的"处理一条诉求"入口。它先开一张任务小票、把编号报给用户，
                再把真正的活交给 _run_core；无论成功还是炸了，都会回写任务状态 ——
                成功记 completed，炸了记 failed 并留下错误原因。

        session_id: 会话隔离键。不传时按客户分桶（cust:<客户名>），
                    保证不同客户之间不会共享对话上下文。

        技术细节：主流程外面套 try/except 是为了保证"异常也必须落一条 failed"。
                  这里只做记录，异常仍原样抛出（不吞），既有行为不变。
        """
        # PHASE 1: 建任务台账（不可用时返回 None，后续所有落库动作自动跳过）
        task_id = self._start_task(user_input)
        try:
            result = self._run_core(user_input, customer, session_id, task_id)
        except Exception as e:
            self._finish_task(task_id, "failed",
                              error_message=f"{type(e).__name__}: {e}")
            raise
        self._finish_task(task_id, "completed", result=result)
        return result

    # ------------------------------------------------------------------
    def _run_core(self, user_input: str, customer: str,
                  session_id: Optional[str], task_id: Optional[str]) -> dict:
        """真正的主流程（即 PHASE 1 之前 run() 的原实现，业务逻辑保持不变）。"""
        sid = session_id or f"cust:{customer}"
        mem = self.sessions.get(sid)
```

### 8.5 ⑤ `result` 回传 task_id

外部拿得到任务编号，可直接去查账。

```python
        result: dict = {"answer": "", "suggestions": [], "pending_approvals": [],
                        "gate_outcomes": [], "trace": None, "session_id": sid,
                        "task_id": task_id}   # PHASE 1: 回传任务编号，便于外部直接查账
```

### 8.6 ⑥ 工具调用透传 task_id

让每一次工具执行都能挂到当前任务上。

```python
            # 执行工具调用
            tool_result = self._dispatch_tool(tool_call, customer, task_id)
```

### 8.7 ⑦ 闸门判定落库

`allowed=False` 或库存闸门拦截 → `status="blocked"`。

```python
        # PHASE 1: 把安全闸门的真实判定落库
        # 大白话：闸门"放行"还是"拦下"都要进账本 —— 被拦下的原因恰恰是审计最想查的。
        # 技术细节：allowed=False 或库存闸门拦截记为 status="blocked"（见 _save_gate_outcomes）。
        self._save_gate_outcomes(task_id, result["gate_outcomes"])
```

### 8.8 ⑧ `_dispatch_tool`：签名加 task_id + 落流水

成功记 success、失败记 failed 并写入失败原因。

```python
    def _dispatch_tool(self, tool_call: dict, customer: str,
                       task_id: Optional[str] = None) -> dict:
        """执行一个工具调用（只读白名单 + 必填参数校验）。

        task_id: PHASE 1 新增，所属任务编号。为 None 时不落库
                 （单测直接调本方法、或持久化不可用时会走到这个分支）。
        """
        name = tool_call["name"]
        try:
            args = json.loads(tool_call.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        start, rec = self.tracer.timer("ticket", name, params=args)

        out: dict
        try:
            handler = self.registry.get(name)
            # 参数校验：必填项缺失直接拒绝（不再只是"注释里写着校验"）
            missing = [k for k in self.registry.required_params(name)
                       if args.get(k) in (None, "")]
            if missing:
                out = {"success": False, "reason": f"缺少必填参数: {missing}"}
            else:
                allowed = self.registry.param_names(name)
                kwargs = {k: v for k, v in args.items() if k in allowed} if allowed else args
                out = handler(**kwargs)
            ok = bool(out.get("success", True))
        except Exception as e:
            out = {"success": False, "reason": f"工具执行异常: {e}"}
            ok = False
        self.tracer.finish(start, rec, result=out, ok=ok)
        # PHASE 1: 每次工具调用都落一条流水 —— 成功记 success、失败记 failed，
        # 并把失败原因写进 error_message（审计要能回答"当时为什么没查出来"）。
        self._save_tool_execution(task_id, name, args, out, ok)
        return out
```

### 8.9 ⑨ 四个持久化辅助方法（追加在 `_inventory_gate` 之后）

统一约定：先判断 task_id → try-except 包裹 → 失败只记日志、不向上抛。

```python
    # PHASE 1: 持久化辅助方法
    # 统一约定：所有落库动作都 (1) 先判断 task_id 是否可用，(2) 用 try-except
    # 包住，(3) 失败只记 ERROR 日志、绝不向上抛 —— 账本写不进去不该让工单停摆。
    # ==================================================================
    def _start_task(self, user_input: str) -> Optional[str]:
        """建立任务台账并返回 task_id；持久化不可用或写入失败时返回 None。

        大白话：开一张任务小票，把编号念给用户听；之后所有流水都挂在这个编号下面。
        技术细节：用 with get_db() 拿会话，正常退出自动 commit；异常被这里吞掉，
                 只记日志，保证"库挂了"等于"暂时不留痕"，而不是"Agent 不能用了"。
        """
        if not self.persist:
            return None
        try:
            with _db_session() as db:
                task = _db_create_task(db, user_input)
                task_id = task.task_id
        except Exception as e:
            log.error("持久化：创建任务失败，本次运行不落库: %s", e)
            return None
        # 控制台按需求显式提示任务编号，用户可据此执行 query_cli.py task <task_id>
        print(f"[任务 ID: {task_id}] 已创建任务，开始处理...")
        return task_id

    def _finish_task(self, task_id: Optional[str], status: str,
                     result: Optional[dict] = None,
                     error_message: Optional[str] = None) -> None:
        """回写任务终态：completed（正常结束）或 failed（异常中断）。"""
        if not (self.persist and task_id):
            return
        try:
            summary = self._build_result_summary(result, error_message)
            with _db_session() as db:
                _db_update_task_status(db, task_id, status, result_summary=summary)
        except Exception as e:
            log.error("持久化：更新任务状态失败(task_id=%s): %s", task_id, e)

    @staticmethod
    def _build_result_summary(result: Optional[dict],
                              error_message: Optional[str] = None) -> dict:
        """把 run() 的返回值压成一份"结果摘要"供落库。

        大白话：只留结论（回答、建议标题、闸门判定、本次耗时指标），
                不把整包原始数据塞进去 —— 摘要是给人快速看的，不是全量备份。
        """
        r = result or {}
        summary: dict = {
            "answer": r.get("answer", ""),
            "session_id": r.get("session_id"),
            "escalation_required": bool(r.get("escalation_required")),
            "suggestions": [
                {
                    "summary": s.get("summary", ""),
                    "actions": [a.get("type") for a in (s.get("actions") or [])],
                }
                for s in (r.get("suggestions") or [])
            ],
            "pending_approvals": [p.get("id") for p in (r.get("pending_approvals") or [])],
            "gate_outcomes": r.get("gate_outcomes") or [],
            "trace": r.get("trace") or {},
        }
        if r.get("llm_error"):
            summary["llm_error"] = r["llm_error"]
        if error_message:
            summary["error"] = error_message
        return summary

    def _save_tool_execution(self, task_id: Optional[str], tool_name: str,
                             params: dict, output: dict, ok: bool,
                             error_message: Optional[str] = None) -> None:
        """记一条工具执行流水。

        技术细节：status 由 ok 推导（success / failed）；失败时若调用方没给原因，
                 自动取工具返回值里的 `reason` 字段，避免库里出现"失败了但没说为什么"。
        """
        if not (self.persist and task_id):
            return
        status = "success" if ok else "failed"
        if error_message is None and not ok:
            error_message = str(output.get("reason") or "") or None
        try:
            with _db_session() as db:
                _db_create_tool_execution(
                    db,
                    task_id=task_id,
                    tool_name=tool_name,
                    input_params=params or {},
                    status=status,
                    output_result=output,
                    error_message=error_message,
                )
        except Exception as e:
            log.error("持久化：写工具流水失败(task_id=%s, tool=%s): %s",
                      task_id, tool_name, e)

    def _save_gate_outcomes(self, task_id: Optional[str],
                            outcomes: list[dict]) -> None:
        """把安全闸门对每个动作的判定落库（**含被拦截的敏感动作**）。

        大白话：闸门说"放行"还是"拦下"都要记一笔。需求里的"发货单被拦截记 blocked"
                在本项目对应的是"退款/补发等敏感动作被闸门拒绝或库存闸门拦下"。

        技术细节：status 映射 ——
            allowed=False              -> "blocked"（参数非法 / 超量 / 超订单金额 / 订单不存在）
            blocked_by == "inventory"  -> "blocked"（补发被库存闸门额外拦下）
            放行并进入人工二次确认      -> "success"（闸门这次调用本身成功了；
                                          output_result.requires_approval=true 表示尚未执行）
            只读动作自动放行            -> "success"

        为什么要循环写而不是只写 blocked：执行链路要完整可追溯，
        "放行了但还没执行" 同样是审计需要看到的状态。

        拦截原因取哪一句：
        - 库存闸门拦截（blocked_by="inventory"）-> 取 action.params 里的 inventory_note。
          注意不能直接用闸门返回的 msg —— 那种情况下 msg 是"动作已进入人工二次确认"，
          会让人误以为是正常审批，而真实原因是库存不足/无法判定 SKU。
        - 参数或业务相对约束拒绝 -> 取闸门返回的 reason（如"退款金额超过订单实付金额"）。
        """
        if not (self.persist and task_id) or not outcomes:
            return
        try:
            # 整批闸门判定用同一个会话写入，减少连接开销
            with _db_session() as db:
                for o in outcomes:
                    params = o.get("params") or {}
                    allowed = bool(o.get("allowed"))
                    blocked_by = str(o.get("blocked_by") or "")
                    blocked = (not allowed) or blocked_by == "inventory"
                    reason: Optional[str] = None
                    if blocked:
                        if blocked_by == "inventory":
                            reason = str(params.get("inventory_note") or "") or None
                        reason = reason or str(o.get("reason") or "") or None
                    _db_create_tool_execution(
                        db,
                        task_id=task_id,
                        tool_name="safety_gate",
                        # 闸门记录必须带 action_type，否则光看参数分不清是退款还是补发
                        input_params={"action_type": o.get("action_type"), **params},
                        status="blocked" if blocked else "success",
                        output_result=o,
                        error_message=reason,
                    )
        except Exception as e:
            log.error("持久化：写闸门判定流水失败(task_id=%s): %s", task_id, e)
```

### 8.10 ⑩ `create_agent` 透传 persistence + 直跑入口

保证便捷函数也能关掉持久化；并给 `python src/agent.py` 一个最小入口。

```python
                 persistence: bool = True) -> TicketAgent:
    return TicketAgent(mock=mock, use_llm=use_llm, session_store=session_store,
                       safety_gate=safety_gate, tracer=tracer,
                       persistence=persistence)


# ----------------------------------------------------------------------
# 直接运行本文件时的最小入口（`python src/agent.py`）
# 正式交互入口仍是 scripts/run.py；这里只保证"直接跑 agent.py 也能启动"。
# ----------------------------------------------------------------------
def _main() -> None:  # pragma: no cover - 手工运行路径
    agent = create_agent(mock=not settings.has_model_key,
                         use_llm=settings.has_model_key)
    result = agent.run("客户反馈上个月买的玻璃水少发了一瓶，要求补发。", customer="张三")
    print("\n--- Agent 回复 ---")
    print(result.get("answer", ""))
    print(f"\n任务编号: {result.get('task_id') or '(未落库)'}")
    if result.get("task_id"):
        print(f"查询详情: python src/query_cli.py task {result['task_id']}")


if __name__ == "__main__":  # pragma: no cover - 手工运行路径
    _main()
```


---

## 9. 配套新增文件

### 9.1 `src/persistence/__init__.py`

```python
# -*- coding: utf-8 -*-
"""持久化层（Phase 1）：SQLite + SQLAlchemy 的任务/工具执行留痕。

为什么单独建一个包而不是 `src/models.py`：
    本项目 `src/models/` 已经是「大模型接入层」包（LLMClient 等）。
    若在 `src/` 下平铺一个 `models.py`，会与 `src/models/` 目录同名冲突
    （Python 包解析优先级会让其中一方失效）。因此把 ORM 模型放进
    本包内，与 domain / tools / safety / execution / memory 等层并列，
    保持项目既有的分层习惯。

目录内容：
- database.py  引擎 / 会话工厂 / get_db() 上下文管理器 / 建表
- models.py    ORM 声明式模型：Task（任务表）、ToolExecution（工具执行表）
- crud.py      所有数据库读写函数（唯一入口，Agent 与 CLI 都走这里）
"""
from __future__ import annotations

from src.persistence.database import DB_PATH, engine, get_db, init_db
from src.persistence.models import Base, Task, ToolExecution

__all__ = [
    "Base",
    "DB_PATH",
    "Task",
    "ToolExecution",
    "engine",
    "get_db",
    "init_db",
]
```

### 9.2 `tests/conftest.py`（测试隔离：不污染真实审计库）

```python
# -*- coding: utf-8 -*-
"""pytest 全局配置。

作用只有一个：**把测试期的持久化数据库重定向到临时目录**。

为什么要这么做：Phase 1 之后 Agent 每跑一次就写一次 SQLite（默认
data/agent_operations.db）。如果不管，`pytest` 一跑就会把测试数据灌进真实审计库，
历史记录里混进一堆 "测试客户" 的任务 —— 审计库最忌讳这个。

做法：在**收集测试模块之前**设置 AGENT_DB_PATH 环境变量，
src/persistence/database.py 会在导入时读取它来定位库文件。
测试自己的用例（含 test_persistence.py）则进一步用 monkeypatch/临时引擎
把自己隔离到各自独立的小库，互不干扰。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="agent-test-db-"))
# setdefault 而不是直接赋值：允许外部显式指定时以外部为准
os.environ.setdefault("AGENT_DB_PATH", str(_TEST_DB_DIR / "agent_operations.db"))
```

### 9.3 `tests/test_persistence.py`

```python
# -*- coding: utf-8 -*-
"""持久化层（Phase 1）测试。

覆盖：
1. Task / ToolExecution 的增查改；
2. JSON 序列化往返 + 超长截断（>10000 字符）；
3. 最近 N 条、按日期查询、按日期统计；
4. 外键约束真的生效（写孤立流水必须失败）；
5. Agent 端到端落库：1 条 task + N 条 tool_execution，状态回到 completed。

隔离原则：所有用例都指向 pytest 的 tmp_path 临时库，
**绝不碰** data/agent_operations.db（那是真实审计数据）。
"""
from __future__ import annotations

import json

import pytest

from src.persistence import crud
from src.persistence.database import build_engine, init_db, make_session_factory
from src.persistence.models import Task, ToolExecution


@pytest.fixture()
def db(tmp_path):
    """每个用例一个全新临时 SQLite 库 + 一个会话。"""
    engine = build_engine(tmp_path / "test_agent_operations.db")
    init_db(engine)
    session = make_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ----------------------------------------------------------------------
# 1) Task 增查改
# ----------------------------------------------------------------------
def test_create_task_generates_readable_task_id(db):
    task = crud.create_task(db, "客户要求补发一瓶玻璃水")
    assert task.id is not None                     # 自增主键已回填
    assert task.task_id.startswith("task_")
    assert len(task.task_id) <= 36                 # 必须塞得进 String(36)
    assert task.status == "processing"
    assert task.user_input == "客户要求补发一瓶玻璃水"
    assert task.created_at is not None
    # 能按 task_id 查回来
    assert crud.get_task_by_id(db, task.task_id).id == task.id


def test_task_ids_are_unique(db):
    ids = {crud.create_task(db, f"诉求{i}").task_id for i in range(20)}
    assert len(ids) == 20


def test_update_task_status_persists_summary(db):
    task = crud.create_task(db, "客户要退款")
    updated = crud.update_task_status(db, task.task_id, "completed",
                                      {"answer": "已生成建议", "gate_outcomes": []})
    assert updated is not None
    assert updated.status == "completed"
    # 摘要是 JSON 字符串，且能解回 dict（中文不能变成 \uXXXX）
    assert json.loads(updated.result_summary)["answer"] == "已生成建议"
    assert "\\u" not in updated.result_summary


def test_update_task_status_unknown_task_returns_none(db):
    """任务不存在时返回 None（打 WARNING），不抛异常 —— 不能让主流程崩。"""
    assert crud.update_task_status(db, "task_not_exist", "completed") is None


def test_update_task_status_accepts_prebuilt_json_string(db):
    """摘要已经是 JSON 字符串时直接存，不二次转义成 "\\"{...}\\""。"""
    task = crud.create_task(db, "x")
    raw = json.dumps({"answer": "ok"}, ensure_ascii=False)
    updated = crud.update_task_status(db, task.task_id, "failed", raw)
    assert updated.result_summary == raw


# ----------------------------------------------------------------------
# 2) ToolExecution + 截断
# ----------------------------------------------------------------------
def test_create_tool_execution_roundtrip(db):
    task = crud.create_task(db, "查订单")
    crud.create_tool_execution(db, task.task_id, "query_order",
                               {"order_id": "PO20260928-00001"}, "success",
                               {"success": True, "order": {"amount": 416.0}})
    rows = crud.get_tool_executions_by_task(db, task.task_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.tool_name == "query_order"
    assert row.status == "success"
    assert row.error_message is None
    assert json.loads(row.input_params)["order_id"] == "PO20260928-00001"
    assert json.loads(row.output_result)["order"]["amount"] == 416.0


def test_oversized_output_is_truncated_with_tag(db):
    """>10000 字符的输出必须截断，并带上「结果已截断」提示。"""
    task = crud.create_task(db, "返回超大结果")
    crud.create_tool_execution(db, task.task_id, "query_order", {"order_id": "X"},
                               "success", {"blob": "x" * 30000})
    row = crud.get_tool_executions_by_task(db, task.task_id)[0]
    assert row.output_result.endswith(crud.TRUNCATED_TAG)
    assert len(row.output_result) == crud.MAX_STORE_CHARS + len(crud.TRUNCATED_TAG)


def test_non_serializable_param_falls_back_to_repr(db):
    """参数含不可序列化对象时降级存 repr，而不是整条记录丢失。"""
    task = crud.create_task(db, "坏参数")
    crud.create_tool_execution(db, task.task_id, "query_order",
                               {"order_id": "X", "obj": object()}, "failed",
                               error_message="测试")
    assert json.loads(crud.get_tool_executions_by_task(db, task.task_id)[0].input_params)


def test_foreign_key_is_enforced(db):
    """外键约束必须真的生效：往不存在 task_id 上写流水要报错。"""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        crud.create_tool_execution(db, "task_not_exist", "query_order", {}, "success")
    db.rollback()


# ----------------------------------------------------------------------
# 3) 列表 / 日期 / 统计
# ----------------------------------------------------------------------
def test_get_recent_tasks_limit_and_order(db):
    for i in range(5):
        crud.create_task(db, f"诉求{i}")
    recent = crud.get_recent_tasks(db, 3)
    assert len(recent) == 3
    # 倒序：最后建的排最前
    assert recent[0].user_input == "诉求4"
    assert [t.user_input for t in recent] == ["诉求4", "诉求3", "诉求2"]


def test_get_recent_tasks_tolerates_bad_limit(db):
    crud.create_task(db, "唯一一条")
    assert len(crud.get_recent_tasks(db, "abc")) == 1   # 非法 limit 回退默认值


def test_get_tasks_by_date(db):
    import datetime

    crud.create_task(db, "今天的任务")
    today = datetime.date.today().isoformat()
    assert len(crud.get_tasks_by_date(db, today)) == 1
    assert crud.get_tasks_by_date(db, "1999-01-01") == []


def test_get_tasks_by_date_rejects_bad_format(db):
    with pytest.raises(ValueError):
        crud.get_tasks_by_date(db, "2024/01/15")


def test_stats_counts_by_status_and_blocked(db):
    import datetime

    t1 = crud.create_task(db, "任务1")
    crud.update_task_status(db, t1.task_id, "completed")
    t2 = crud.create_task(db, "任务2")           # 保持 processing
    crud.create_tool_execution(db, t1.task_id, "query_order", {}, "success", {})
    crud.create_tool_execution(db, t1.task_id, "safety_gate", {}, "blocked", {}, "超量")
    crud.create_tool_execution(db, t2.task_id, "query_order", {}, "failed", {}, "订单不存在")

    stats = crud.get_stats(db, datetime.date.today().isoformat())
    assert stats["total"] == 2
    assert stats["by_status"] == {"completed": 1, "processing": 1}
    assert stats["tool_calls"] == 3
    assert stats["blocked"] == 1
    assert stats["tool_failed"] == 1


# ----------------------------------------------------------------------
# 4) Agent 端到端落库
# ----------------------------------------------------------------------
@pytest.fixture()
def temp_agent_db(tmp_path, monkeypatch):
    """把 agent 模块用的会话工厂换成临时库，避免污染真实审计库。"""
    import src.agent as agent_mod

    engine = build_engine(tmp_path / "agent_e2e.db")
    init_db(engine)
    factory = make_session_factory(engine)

    from contextlib import contextmanager

    @contextmanager
    def _get_db():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(agent_mod, "_db_session", _get_db)
    monkeypatch.setattr(agent_mod, "PERSISTENCE_AVAILABLE", True)
    yield factory
    engine.dispose()


def test_agent_run_persists_task_and_executions(temp_agent_db):
    """跑一次任务：应产生 1 条 task（completed）+ N 条 tool_execution。"""
    from src.agent import create_agent

    agent = create_agent(mock=True)
    out = agent.run("订单 PO20260928-00001 申请退款 4999 元。", customer="测试客户")

    task_id = out["task_id"]
    assert task_id and task_id.startswith("task_")

    db = temp_agent_db()
    try:
        task = crud.get_task_by_id(db, task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.user_input.startswith("订单 PO20260928-00001")
        assert json.loads(task.result_summary)["answer"]

        execs = crud.get_tool_executions_by_task(db, task_id)
        assert execs, "任务应至少落一条工具/闸门流水"
        # 闸门判定必须落库，且被拒绝的动作标记为 blocked
        gate_rows = [e for e in execs if e.tool_name == "safety_gate"]
        assert gate_rows and gate_rows[0].status == "blocked"
        assert gate_rows[0].error_message
    finally:
        db.close()


def test_agent_run_failure_marks_task_failed(temp_agent_db, monkeypatch):
    """主流程抛异常时任务必须落 failed 并带错误信息（异常仍向上抛）。"""
    from src.agent import create_agent

    agent = create_agent(mock=True)

    def _boom(*args, **kwargs):
        raise RuntimeError("模拟主流程崩溃")

    monkeypatch.setattr(agent, "_build_suggestions", _boom)
    with pytest.raises(RuntimeError):
        agent.run("随便一条诉求", customer="测试客户")

    db = temp_agent_db()
    try:
        latest = crud.get_recent_tasks(db, 1)[0]
        assert latest.status == "failed"
        assert "模拟主流程崩溃" in latest.result_summary
    finally:
        db.close()


def test_agent_persistence_disabled_writes_nothing(temp_agent_db):
    """persistence=False 时应完全不落库（单测保持库干净的开关）。"""
    from src.agent import create_agent

    agent = create_agent(mock=True, persistence=False)
    out = agent.run("订单 PO20260928-00001 少发了一瓶。", customer="测试客户")
    assert out["task_id"] is None

    db = temp_agent_db()
    try:
        assert crud.get_recent_tasks(db, 10) == []
    finally:
        db.close()
```

---

## 10. 验收结果（全部实测通过）

| 验收项 | 结果 |
|---|---|
| 运行入口正常启动、功能不变 | ✅ `python scripts/run.py`（mock）与 `python src/agent.py` 均正常；原有 **71 项测试全部通过** |
| 处理任务后自动生成 `data/agent_operations.db` | ✅ 删库后跑一次入口，库自动重建（`init_db()` 在导入时幂等建表） |
| 库里有 1 条 task + N 条 tool_execution | ✅ 实测 1 条 task（completed）+ N 条流水（N = 本次工具调用数 + 闸门判定数） |
| `query_cli.py recent 5` 能看到刚才的任务 | ✅ |
| `query_cli.py task <task_id>` 能看到完整执行链路 | ✅ 含每次调用的入参 / 结果 / 错误与闸门判定 |
| 原有测试全部通过 | ✅ `88 passed`（71 原有 + 17 新增） |

```
$ ./.venv/Scripts/python.exe -m pytest -q
88 passed in 41.73s
```

一条含真实工具调用的执行链路（`python src/query_cli.py task <task_id>` 截取）：

```text
--- 执行链路（共 3 条） ---
+---+---------------------+----------------+-----------------+-----------------------------+--------------------------+
| # | 执行时间            | 工具/环节      | 状态            | 输入参数                    | 结果/错误                |
+---+---------------------+----------------+-----------------+-----------------------------+--------------------------+
| 1 | 2026-09-30 12:41:59 | query_order    | 成功(success)   | {"order_id": "PO20260928-…  | {"success": true, "order…|
| 2 | 2026-09-30 12:41:59 | query_check    | 失败(failed)    | {}                          | 工具执行异常: '未知工具… |
| 3 | 2026-09-30 12:41:59 | safety_gate    | 已拦截(blocked) | {"action_type": "refund",…  | 参数校验失败：退款金额 … |
+---+---------------------+----------------+-----------------+-----------------------------+--------------------------+
```

`python src/query_cli.py stats today`：

```text
====================================================
处理量统计  2026-09-30（今天）
====================================================
任务总数      : 26
  - 已完成(completed): 26
工具执行次数  : 21
安全闸门拦截  : 14
工具执行失败  : 2
```

---

## 11. 设计说明（几个刻意的取舍）

1. **持久化是增强项，不是启动前提。** 未安装 sqlalchemy 时自动降级为「不落库」并打 WARNING，
   Agent 照常工作；所有落库动作都包在 `try-except` 里，写库失败只记 ERROR 日志，不打断工单处理。
2. **失败也必须留痕。** 工具报错记 `failed` + 原因；被安全闸门拒绝记 `blocked` + 原因。
   审计最关心的恰恰是「当时为什么没成」。
3. **`allowed=False`（闸门拒绝）与「待人工确认」区分开。** 前者是 `blocked`；
   后者是闸门放行、等人工审批，记 `success`（闸门这次调用本身是成功的，
   `output_result.requires_approval=true` 表明尚未执行）。
4. **超长结果截断在字符串层**（10000 字符 + `…[结果已截断]`）。截断后不再保证是合法 JSON ——
   刻意的取舍：「看到前 10000 字」比「格式完整但什么都没存下」对审计更有用。
5. **外键真的生效。** SQLite 默认不强制外键，已在每条连接上打开 `PRAGMA foreign_keys=ON`，
   杜绝 `tool_executions` 里出现指向不存在任务的孤儿记录。
6. **测试不污染审计库。** `tests/conftest.py` 在收集用例前把库路径重定向到临时目录，
   测试自身再用独立临时引擎隔离。实测跑完 88 项测试后，真实库任务数不变。
7. **`task_id` 用 `task_` + uuid4 前 16 位**（共 21 字符，塞得进 `String(36)`），
   兼顾「UUID 派生、几乎不重复」与「日志里可读、能口头传递」。
