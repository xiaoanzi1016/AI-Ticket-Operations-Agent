# -*- coding: utf-8 -*-
"""Web 服务层（PHASE 2）。

【这一层是干什么的】
把原本只能在命令行里跑的 Agent 包装成 HTTP 服务，让多人/多系统都能提交工单、
查询处理结果。它**不重新实现业务逻辑**，只是给 Phase 1 的 Agent 加一层"接入壳"：
  路由接收请求 → 上传文件落临时目录 → 建任务记录 → 丢进异步队列 → 立刻返回 task_id
  后台 Worker 从队列取任务 → 线程池里跑 Agent.run() → 回写任务状态

【目录结构】
  settings.py   配置（pydantic-settings）
  models.py     Pydantic 请求/响应模型
  deps.py       依赖注入（数据库会话 / 后台 Worker）
  dataset.py    上传文件 → 临时目录 → 任务级数据源
  worker.py     异步队列 + 后台 Worker + 取消标志
  main.py       FastAPI 应用实例（CORS / 异常处理 / lifespan）
  routers/      system.py（健康检查）、tasks.py（任务增查与取消）

【为什么这里要先导入 settings】
settings.py 在导入时会把 DEEPSEEK_* 别名回填成 MODEL_*（本项目 config.py 读的名字）。
必须保证它早于 src.config 被导入，否则大模型读取 key 时拿到的是空值。
放在包 __init__ 里是最省心、也最不容易被漏掉的做法。
"""
from src.api.settings import api_settings as api_settings  # noqa: F401  (导入即完成别名回填)

__all__ = ["api_settings"]
