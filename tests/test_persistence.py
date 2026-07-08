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
