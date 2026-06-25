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
