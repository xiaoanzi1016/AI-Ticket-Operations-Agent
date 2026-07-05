# -*- coding: utf-8 -*-
"""安全闸门核心测试（项目差异化所在）。

说明：闸门现在支持注入 order_lookup。单测用假订单，只验证闸门自身逻辑，
不依赖真实数据源；针对真实数据的场景演示见 scripts/demo_security.py。
"""
from types import SimpleNamespace

import pytest

from src.domain.models import Action, ActionType, RiskLevel
from src.safety.gate import SafetyGate


def _order(order_id="PO1", amount=10000.0, status="已发货"):
    """构造一个假订单（只需闸门用到的字段）。"""
    return SimpleNamespace(
        order_id=order_id,
        amount=amount,
        status=status,
        items=[{"sku": "SKU-1", "name": "商品", "spec": "标准", "price": 1.0, "qty": 1}],
    )


@pytest.fixture
def gate():
    """注入假订单查询：单测只关心闸门逻辑，不依赖真实数据源。"""
    return SafetyGate(order_lookup=lambda oid: _order(oid) if oid == "PO1" else None)


# ----------------------------------------------------------------------
# 白名单 + 二次确认
# ----------------------------------------------------------------------
def test_remove_sensitive_action_requires_approval(gate):
    """敏感动作（退款）应进入二次确认，而非直接放行。"""
    act = Action(ActionType.REFUND, {"amount": 100, "order_id": "PO1"}, RiskLevel.HIGH)
    ok, req, msg = gate.check(act, "T1")
    assert ok is True
    assert req is not None          # 必须待确认
    assert req.status == "pending"


def test_sensitive_action_cannot_bypass(gate):
    """绕过闸门：未确认前，动作绝不能执行。只有 approved 后才算通过。"""
    act = Action(ActionType.REFUND, {"amount": 100, "order_id": "PO1"}, RiskLevel.HIGH)
    ok, req, _ = gate.check(act, "T1")
    assert req is not None
    assert req.status != "approved"


def test_unknown_action_rejected(gate):
    """白名单外动作（非法类型）-> 拒绝。"""
    # VOID_TICKET 是在白名单内的敏感动作（会进二次确认），
    # 真正"白名单外"的是未在 ACTION_RISK 里定义的类型。
    act = Action(ActionType.VOID_TICKET, {}, RiskLevel.HIGH)
    ok, _, msg = gate.check(act, "T1")
    assert ok is True                      # 白名单内敏感动作 -> 进确认（不直接执行）
    assert msg is not None

    rogue = Action("hack_action", {"amount": 9999}, RiskLevel.HIGH)
    ok2, _, msg2 = gate.check(rogue, "T1")
    assert ok2 is False
    assert "白名单" in msg2


def test_readonly_query_allowed_automatic(gate):
    """只读/建议动作无需确认，直接放行。"""
    act = Action(ActionType.QUERY, {}, RiskLevel.LOW)
    ok, req, _ = gate.check(act, "T1")
    assert ok is True
    assert req is None


# ----------------------------------------------------------------------
# 参数格式 / 范围
# ----------------------------------------------------------------------
def test_negative_refund_rejected(gate):
    act = Action(ActionType.REFUND, {"amount": -1, "order_id": "PO1"}, RiskLevel.HIGH)
    ok, _, msg = gate.check(act, "T1")
    assert ok is False
    assert "正数" in msg


def test_refund_over_limit_rejected(gate):
    act = Action(ActionType.REFUND, {"amount": 99999, "order_id": "PO1"}, RiskLevel.HIGH)
    ok, _, msg = gate.check(act, "T1")
    assert ok is False
    assert "上限" in msg


def test_missing_critical_param_rejected(gate):
    """关键参数缺失 -> 明确拒绝（不再误报格式错误）。"""
    act = Action(ActionType.REFUND, {"note": "无金额"}, RiskLevel.HIGH)
    ok, _, msg = gate.check(act, "T1")
    assert ok is False
    assert "缺失" in msg


# ----------------------------------------------------------------------
# 参数名规范化（LLM 别名攻击防护）
# ----------------------------------------------------------------------
def test_quantity_alias_normalized(gate):
    """LLM 输出 quantity 别名 -> 应被规范化并拦截超限。"""
    act = Action(ActionType.REISSUE, {"quantity": 9999, "order_id": "PO1"}, RiskLevel.HIGH)
    ok, _, msg = gate.check(act, "T1")
    assert ok is False
    assert "上限" in msg


def test_count_alias_normalized(gate):
    act = Action(ActionType.REISSUE, {"count": 9999, "order_id": "PO1"}, RiskLevel.HIGH)
    ok, _, msg = gate.check(act, "T1")
    assert ok is False


def test_money_alias_normalized(gate):
    """money/price 别名 -> 规范化到 amount 并拦截超限退款。"""
    act = Action(ActionType.REFUND, {"money": 99999, "order_id": "PO1"}, RiskLevel.HIGH)
    ok, _, _ = gate.check(act, "T1")
    assert ok is False


# ----------------------------------------------------------------------
# 参数宽容化 / 别名冲突（本轮新增）
# ----------------------------------------------------------------------
def test_string_number_accepted(gate):
    """LLM 把数量输出成字符串（"1"）时应宽容解析，而不是误判"格式错误"。"""
    act = Action(ActionType.REISSUE, {"qty": "1", "order_id": "PO1"}, RiskLevel.HIGH)
    ok, req, msg = gate.check(act, "T1")
    assert ok is True, msg
    assert req is not None


def test_string_number_still_range_checked(gate):
    """宽容解析之后，范围校验依然生效。"""
    act = Action(ActionType.REISSUE, {"qty": "99999", "order_id": "PO1"}, RiskLevel.HIGH)
    ok, _, msg = gate.check(act, "T1")
    assert ok is False
    assert "上限" in msg


def test_alias_conflict_rejected(gate):
    """同义字段同时出现且值不一致 -> 明确拒绝（不再静默取其一）。"""
    act = Action(ActionType.REISSUE, {"qty": 1, "quantity": 9999, "order_id": "PO1"},
                 RiskLevel.HIGH)
    ok, _, msg = gate.check(act, "T1")
    assert ok is False
    assert "冲突" in msg


# ----------------------------------------------------------------------
# 业务相对约束（本轮新增：原实现只校验绝对上限 5000）
# ----------------------------------------------------------------------
def test_refund_exceeds_order_amount_rejected():
    """15 元的订单申请退款 4999 元 -> 必须拒绝（原实现会放行）。"""
    g = SafetyGate(order_lookup=lambda oid: _order(oid, amount=15.0))
    act = Action(ActionType.REFUND, {"amount": 4999.0, "order_id": "PO1"}, RiskLevel.HIGH)
    ok, _, msg = g.check(act, "T1")
    assert ok is False
    assert "超过订单实付" in msg


def test_refund_equal_order_amount_allowed():
    """退款金额等于订单实付 -> 通过校验并进入人工确认。"""
    g = SafetyGate(order_lookup=lambda oid: _order(oid, amount=416.0))
    act = Action(ActionType.REFUND, {"amount": 416.0, "order_id": "PO1"}, RiskLevel.HIGH)
    ok, req, msg = g.check(act, "T1")
    assert ok is True, msg
    assert req is not None


def test_refund_unknown_order_rejected(gate):
    """订单不存在 -> 拒绝（防止对不存在订单发起退款）。"""
    act = Action(ActionType.REFUND, {"amount": 10, "order_id": "PO-NOT-EXIST"}, RiskLevel.HIGH)
    ok, _, msg = gate.check(act, "T1")
    assert ok is False
    assert "不存在" in msg


def test_refund_blocked_for_non_refundable_status():
    """订单状态不支持退款（已取消）-> 拒绝。"""
    g = SafetyGate(order_lookup=lambda oid: _order(oid, status="已取消"))
    act = Action(ActionType.REFUND, {"amount": 10, "order_id": "PO1"}, RiskLevel.HIGH)
    ok, _, msg = g.check(act, "T1")
    assert ok is False
    assert "状态" in msg


def test_reissue_blocked_for_cancelled_order():
    """已取消订单不允许补发。"""
    g = SafetyGate(order_lookup=lambda oid: _order(oid, status="已取消"))
    act = Action(ActionType.REISSUE, {"qty": 1, "order_id": "PO1"}, RiskLevel.HIGH)
    ok, _, msg = g.check(act, "T1")
    assert ok is False
    assert "状态" in msg


# ----------------------------------------------------------------------
# 人工确认 + 审计留痕（方向 D：审批控制台的数据源）
# ----------------------------------------------------------------------
def test_approve_flow(gate):
    """人工批准后动作进入可执行状态。"""
    act = Action(ActionType.REFUND, {"amount": 100, "order_id": "PO1"}, RiskLevel.HIGH)
    _, req, _ = gate.check(act, "T1")
    ok, msg = gate.approve(req.req_id, "客服A")
    assert ok is True
    assert req.status == "approved"


def test_deny_flow(gate):
    """人工驳回后动作不可执行。"""
    act = Action(ActionType.REISSUE, {"qty": 1, "order_id": "PO1"}, RiskLevel.HIGH)
    _, req, _ = gate.check(act, "T1")
    ok, _ = gate.deny(req.req_id, "客服B", "客户信息存疑")
    assert ok is True
    assert req.status == "denied"


def test_approve_and_audit(gate):
    """批准后动作状态变化 + 审计留痕生成。"""
    act = Action(ActionType.REFUND, {"amount": 100, "order_id": "PO1"}, RiskLevel.HIGH)
    ok, req, _ = gate.check(act, "T-AUDIT-1")
    assert req is not None

    ok, msg = gate.approve(req.req_id, "李运营")
    assert ok and "批准" in msg

    pend = gate.pending_dicts()
    assert all(p["status"] != "pending" for p in pend) or not pend
    rows = gate.audit_dicts()
    assert len(rows) == 1
    assert rows[0]["operator"] == "李运营"
    assert rows[0]["decision"] == "approved"
    assert rows[0]["ticket_id"] == "T-AUDIT-1"
    import json as _json

    assert _json.loads(rows[0]["params"])["amount"] == 100


def test_deny_and_audit(gate):
    """驳回后留痕 decision=denied + 原因。"""
    act = Action(ActionType.REISSUE, {"qty": 1, "order_id": "PO1"}, RiskLevel.HIGH)
    _, req, _ = gate.check(act, "T-AUDIT-2")
    ok, msg = gate.deny(req.req_id, "王主管", "与仓库确认无少发")
    assert ok and "驳回" in msg
    rows = gate.audit_dicts()
    assert rows[0]["decision"] == "denied"
    assert "无少发" in rows[0]["note"]


def test_pending_dicts_serializable(gate):
    """pending_dicts 可 JSON 序列化（供控制台/界面用）。"""
    import json as _json

    act = Action(ActionType.REFUND, {"amount": 100, "order_id": "PO1"}, RiskLevel.HIGH)
    _, req, _ = gate.check(act, "T-AUDIT-3")
    d = gate.pending_dicts()
    assert len(d) == 1
    s = _json.dumps(d, ensure_ascii=False)   # 不抛异常即通过
    assert req.ticket_id in s or "refund" in s


# ----------------------------------------------------------------------
# 稳定编号 + 会话隔离（本轮新增：原来用 id()，是内存地址）
# ----------------------------------------------------------------------
def test_req_id_is_stable_and_usable(gate):
    """待确认请求用稳定编号 AP-xxxx，且可用它批准/查询。"""
    act = Action(ActionType.REFUND, {"amount": 100, "order_id": "PO1"}, RiskLevel.HIGH)
    _, req, _ = gate.check(act, "T-ID")
    assert req.req_id.startswith("AP-")
    assert gate.get_request(req.req_id) is req
    assert gate.pending_dicts()[0]["id"] == req.req_id
    ok, _ = gate.approve(req.req_id, "客服C")
    assert ok is True


def test_pending_isolated_by_session(gate):
    """待确认队列按 session 隔离，跨会话不可见。"""
    a = Action(ActionType.REFUND, {"amount": 100, "order_id": "PO1"}, RiskLevel.HIGH)
    b = Action(ActionType.REISSUE, {"qty": 1, "order_id": "PO1"}, RiskLevel.HIGH)
    gate.check(a, "T-A", session_id="s1")
    gate.check(b, "T-B", session_id="s2")

    assert len(gate.pending_dicts()) == 2
    assert len(gate.pending_dicts(session_id="s1")) == 1
    assert gate.pending_dicts(session_id="s1")[0]["ticket_id"] == "T-A"
    assert gate.list_pending("s2")[0].ticket_id == "T-B"


def test_mark_executed_writes_audit(gate):
    """执行器标记已执行后，审计里出现 decision=executed。"""
    act = Action(ActionType.REFUND, {"amount": 100, "order_id": "PO1"}, RiskLevel.HIGH)
    _, req, _ = gate.check(act, "T-EXEC")
    gate.approve(req.req_id, "客服D")
    assert gate.mark_executed(req.req_id, "系统执行器", "已退款 100 元") is True
    assert req.status == "executed"
    rows = gate.audit_dicts()
    assert rows[-1]["decision"] == "executed"
    assert "已退款" in rows[-1]["note"]
