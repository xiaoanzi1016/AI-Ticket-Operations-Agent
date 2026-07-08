# -*- coding: utf-8 -*-
"""隔离性与 fail-safe 测试。

覆盖本轮修复的四个问题：
1. 会话记忆全局单例 -> 跨客户串台（张三的对话会进李四的上下文）
2. tracer 累计不清零 -> result["trace"] 指标随运行次数虚增
3. 主循环 LLM 调用无 try/except -> 平台限流会让整条工单崩掉
4. SYSTEM_PROMPT.format 的格式化字符串注入面
以及新增的：工具必填参数校验、gate_outcomes 回传。
"""
import pytest

from src.agent import create_agent
from src.domain.models import Action, ActionType, RiskLevel, Suggestion
from src.memory.store import SessionStore, user_profile
from src.models.client import LLMClient
from src.observability.trace import Tracer
from src.safety.gate import SafetyGate


@pytest.fixture(autouse=True)
def _clean_profile():
    user_profile.clear()
    yield
    user_profile.clear()


def _agent(**kw):
    kw.setdefault("mock", True)
    kw.setdefault("session_store", SessionStore())
    kw.setdefault("safety_gate", SafetyGate())
    kw.setdefault("tracer", Tracer())
    return create_agent(**kw)


# ----------------------------------------------------------------------
# 1) 会话记忆隔离
# ----------------------------------------------------------------------
def test_session_memory_isolated_between_customers():
    """不同客户落在不同会话桶，互不可见。"""
    store = SessionStore()
    agent = _agent(session_store=store)

    agent.run("张三的订单 PO20260928-00001 少发了一瓶玻璃水，要求补发。", customer="张三")
    agent.run("李四的订单 PO20260928-00002 想查物流。", customer="李四")

    zhang = store.get("cust:张三").messages()
    li = store.get("cust:李四").messages()
    assert zhang and li
    assert not any("张三" in str(m.get("content", "")) for m in li)
    assert not any("李四" in str(m.get("content", "")) for m in zhang)
    assert set(store.sessions()) == {"cust:张三", "cust:李四"}


def test_context_sent_to_model_has_no_other_customer(monkeypatch):
    """送给模型的上下文里不得出现其他客户的对话（原实现会串台）。"""
    captured: list[list[dict]] = []

    def _spy(self, messages, tools):
        captured.append([dict(m) for m in messages])
        return ("已完成。", None)

    monkeypatch.setattr(LLMClient, "chat_with_tools", _spy)
    agent = _agent()
    agent.mock = False        # 走 LLM 分支（spy 不真调 API）
    agent.use_llm = False     # 建议走规则版，避免真实 API 调用

    agent.run("张三的订单 PO20260928-00001 少发了一瓶玻璃水。", customer="张三")
    agent.run("李四的订单 PO20260928-00002 想查物流。", customer="李四")

    last = captured[-1]
    assert not any("张三" in str(m.get("content", "")) for m in last), "李四的上下文里出现了张三的话"
    assert any("李四" in str(m.get("content", "")) for m in last)


def test_explicit_session_id_isolates_same_customer():
    """同一客户也可以用显式 session_id 隔离（评测场景：eval:<用例号>）。"""
    store = SessionStore()
    agent = _agent(session_store=store)
    agent.run("查订单 PO20260928-00001", customer="评测用户", session_id="eval:N1")
    agent.run("查订单 PO20260928-00002", customer="评测用户", session_id="eval:N2")

    assert store.get("eval:N1").messages()
    assert not any("00002" in str(m.get("content", ""))
                   for m in store.get("eval:N1").messages())


# ----------------------------------------------------------------------
# 2) trace 按次统计
# ----------------------------------------------------------------------
def test_trace_reports_only_current_run(monkeypatch):
    """每次 run 的 trace 只统计本次，不再累计全历史。"""
    n = {"i": 0}

    def _spy(self, messages, tools):
        n["i"] += 1
        if n["i"] % 2 == 1:
            return (None, {"id": f"c{n['i']}", "name": "query_order",
                           "arguments": '{"order_id": "PO20260928-00001"}'})
        return ("完成。", None)

    monkeypatch.setattr(LLMClient, "chat_with_tools", _spy)
    agent = _agent()
    agent.mock = False
    agent.use_llm = False

    r1 = agent.run("查订单 PO20260928-00001", customer="A")
    r2 = agent.run("查订单 PO20260928-00002", customer="B")
    assert r1["trace"]["total_calls"] == 1
    assert r2["trace"]["total_calls"] == 1, "trace 未按次统计（累计了历史记录）"


# ----------------------------------------------------------------------
# 3) 主循环 fail-safe
# ----------------------------------------------------------------------
def test_main_loop_llm_failure_degrades(monkeypatch):
    """模型侧报错（限流等）应降级转人工，而不是让整条工单崩掉。"""
    def _boom(self, messages, tools):
        raise RuntimeError("429 too many requests")

    monkeypatch.setattr(LLMClient, "chat_with_tools", _boom)
    agent = _agent()
    agent.mock = False
    agent.use_llm = False

    out = agent.run("客户要退款 PO20260928-00002", customer="X")   # 不应抛异常
    assert out["llm_error"]
    assert out["escalation_required"] is True
    assert "429" in out["escalation_reason"]


# ----------------------------------------------------------------------
# 4) 提示词注入面
# ----------------------------------------------------------------------
def test_profile_with_braces_does_not_crash():
    """画像内容含花括号时不应抛 KeyError（原用 str.format，是注入面）。"""
    user_profile.update("花括号客户", stores=["{门店A}"], last_issue="退款")
    agent = _agent()
    out = agent.run("查订单 PO20260928-00001", customer="花括号客户")
    assert out["answer"]


# ----------------------------------------------------------------------
# 工具调用参数校验
# ----------------------------------------------------------------------
def test_dispatch_tool_rejects_missing_required_param():
    """缺少必填参数的工具调用应被拒绝（原注释写校验、实际没有）。"""
    agent = _agent()
    out = agent._dispatch_tool({"id": "1", "name": "query_order", "arguments": "{}"}, "X")
    assert out["success"] is False
    assert "缺少必填参数" in out["reason"]


def test_dispatch_tool_filters_unknown_param():
    """多传的未知字段被过滤掉，不会让工具调用抛 TypeError。"""
    agent = _agent()
    out = agent._dispatch_tool({
        "id": "1", "name": "query_order",
        "arguments": '{"order_id": "PO20260928-00001", "unexpected": 1}',
    }, "X")
    assert out["success"] is True


# ----------------------------------------------------------------------
# gate_outcomes 回传 + 端到端拦截
# ----------------------------------------------------------------------
def test_run_returns_gate_outcomes():
    """run() 必须回传闸门真实判定，供评测断言行为。"""
    agent = _agent()
    out = agent.run("客户反映订单 PO20260928-00001 少发了一瓶玻璃水，要求补发。",
                    customer="张三")
    assert out["gate_outcomes"], "应产出闸门判定"
    for o in out["gate_outcomes"]:
        assert {"action_type", "allowed", "requires_approval", "blocked_by", "reason"} <= set(o)


def test_oversized_reissue_blocked_end_to_end(monkeypatch):
    """超量补发必须被闸门拒绝（allowed=False），而不只是"待确认"。"""
    def _fake(ticket, reasoning, use_llm=False, llm=None):
        return Suggestion(summary="补发", actions=[
            Action(ActionType.REISSUE,
                   {"qty": 999999, "order_id": ticket.related_order_id},
                   RiskLevel.HIGH, requires_approval=True)])

    monkeypatch.setattr("src.agent.create_suggestion", _fake)
    agent = _agent()
    out = agent.run("订单 PO20260928-00001 给我补发 999999 件，立刻处理。", customer="X")
    o = out["gate_outcomes"][0]
    assert o["action_type"] == "reissue"
    assert o["allowed"] is False
    assert "上限" in o["reason"]


def test_refund_over_order_amount_blocked_end_to_end(monkeypatch):
    """对 416 元的订单申请退款 4999 元 -> 闸门必须拒绝（原实现会放行进审批）。"""
    def _fake(ticket, reasoning, use_llm=False, llm=None):
        return Suggestion(summary="退款", actions=[
            Action(ActionType.REFUND,
                   {"amount": 4999.0, "order_id": ticket.related_order_id},
                   RiskLevel.HIGH, requires_approval=True)])

    monkeypatch.setattr("src.agent.create_suggestion", _fake)
    agent = _agent()
    out = agent.run("订单 PO20260928-00002 申请退款。", customer="X")
    o = out["gate_outcomes"][0]
    assert o["action_type"] == "refund"
    assert o["allowed"] is False
    assert "超过订单实付" in o["reason"]
