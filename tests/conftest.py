# -*- coding: utf-8 -*-
"""pytest 全局配置。

作用有两个：**把测试期的持久化数据库重定向到临时目录**、**强制 Agent 走 mock 模式**。

为什么要重定向数据库：Phase 1 之后 Agent 每跑一次就写一次 SQLite（默认
data/agent_operations.db）。如果不管，`pytest` 一跑就会把测试数据灌进真实审计库，
历史记录里混进一堆 "测试客户" 的任务 —— 审计库最忌讳这个。

为什么要强制 mock（AGENT_FORCE_MOCK）：项目的 .env 里通常配了真实的模型 key，
不拦一下，Phase 2 的 API 测试每提交一个任务就会真的去调一次大模型 ——
既慢、又要花钱、还依赖网络，测试结果不可复现。

做法：在**收集测试模块之前**设置这两个环境变量。
- AGENT_DB_PATH   由 src/persistence/database.py 在导入时读取；
- AGENT_FORCE_MOCK 由 src/api/settings.py 读取（api_settings.agent_force_mock）。
测试自己的用例（含 test_persistence.py / test_api.py）会进一步用临时引擎
把自己隔离到各自独立的小库，互不干扰。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="agent-test-db-"))
# setdefault 而不是直接赋值：允许外部显式指定时以外部为准
os.environ.setdefault("AGENT_DB_PATH", str(_TEST_DB_DIR / "agent_operations.db"))
# PHASE 2：测试一律不调真实大模型（有 .env key 也不会被用上）
os.environ.setdefault("AGENT_FORCE_MOCK", "1")
# 接口鉴权：测试环境固定一个 Token，让任务接口走"已鉴权"分支。
# 不设的话 verify_token 会直接放行，鉴权这条路径就永远覆盖不到 ——
# 等于测试默认跑在"生产不会出现的配置"下。
os.environ.setdefault("API_AUTH_TOKEN", "test-token")

