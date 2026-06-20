# -*- coding: utf-8 -*-
"""依赖注入（PHASE 2 新增）。

【这个文件是干什么的】
FastAPI 的 `Depends(...)` 机制需要"提供者函数"。这里集中放三个：
- `get_db()`      —— 给路由一个数据库会话，请求结束自动提交/回滚/关闭
- `get_worker()`  —— 给路由后台任务队列
- `get_settings()`—— 给路由读取当前配置

【为什么要走依赖注入，而不是在路由里直接 import 全局对象】
1. **测试可替换**：`app.dependency_overrides[get_db] = fake_db` 一行就能把
   真实数据库换成测试库，不用改业务代码；
2. **生命周期正确**：FastAPI 会在响应结束后自动执行生成器的收尾逻辑，
   保证会话一定被 close（漏 close 会把连接池占满）；
3. **显式声明依赖**：看函数签名就知道这个接口需要什么，比翻全局变量清楚。
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from src.api.settings import ApiSettings, api_settings
from src.api.worker import TaskWorker, task_worker
from src.persistence.database import get_db as _session_scope


def get_settings() -> ApiSettings:
    """提供当前生效的 API 配置。"""
    return api_settings


def get_db() -> Iterator[Session]:
    """提供数据库会话（FastAPI 依赖）。

    大白话：把一个请求要用到的数据库连接交出去，请求处理完自动归还。

    技术细节：这里刻意**沿用 Phase 1 `get_db()` 的上下文管理器模式**，
    只是把它改造成 FastAPI 需要的「生成器依赖」形状：
    yield 之前是准备，yield 之后的代码由框架在响应结束后执行。
    所以 Phase 1 那套 "正常 commit / 异常 rollback / 一定 close" 的语义完全保留。

    用法：
        def endpoint(db: Session = Depends(get_db)):
            ...
    """
    with _session_scope() as db:
        yield db


def get_worker() -> TaskWorker:
    """提供后台任务队列（Worker）。"""
    return task_worker
