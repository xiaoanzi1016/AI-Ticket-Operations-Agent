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
