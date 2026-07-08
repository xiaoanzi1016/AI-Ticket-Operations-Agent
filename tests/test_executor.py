# -*- coding: utf-8 -*-
"""执行器测试：批准后才执行、幂等、执行前复核。

本轮新增 —— 原实现里 `SafetyGate.approve()` 只改内存状态位，没有任何代码真的
退款/发货（"有闸门、没有门后的执行"），库存也不扣减（可被反复批准补发）。
"""
import pytest

from src.config import settings
from src.data_source import DataSource
from src.domain.models import Action, ActionType, RiskLevel
from src.execution.executor import ActionExecutor
from src.safety.gate import SafetyGate

REFUNDABLE = ("已付款待发货", "已发货", "已完成")


@pytest.fixture
def env():
    """独立的数据源 + 闸门 + 执行器，互不污染全局单例。"""
    ds = DataSource()
    ds.load()
    gate = SafetyGate(order_lookup=ds.get_order)
    return ds, gate, ActionExecutor(gate=gate, data_source=ds)


def _refundable_order(ds, limit: float | None = None):
    """找一个可退款且金额不超绝对上限的订单。"""
    cap = settings.refund_max_amount if limit is None else limit
    for o in ds.all_orders():
        if 0 < o.amount <= cap and o.status in REFUNDABLE:
            return o
    return None


def _single_sku_with_stock(ds, need=2):
    for o in ds.all_orders():
        if o.status not in REFUNDABLE:
            continue
        skus = {it["sku"] for it in o.items if it.get("sku")}
        if len(skus) != 1:
            continue
        sku = next(iter(skus))
        inv = ds.get_inventory(o.store, sku)
        if inv and inv["available"] >= need:
            return o, sku
    return None, None


# ----------------------------------------------------------------------
# 状态门
# ----------------------------------------------------------------------
def test_executor_refuses_unapproved(env):
    """未获人工批准的动作，执行器必须拒绝。"""
    ds, gate, ex = env
    order = _refundable_order(ds)
    act = Action(ActionType.REFUND, {"amount": 1.0, "order_id": order.order_id}, RiskLevel.HIGH)
    ok, req, _ = gate.check(act, "T-1")
    assert ok and req is not None

    res = ex.execute(req)      # 仍是 pending，未批准
    assert res["success"] is False
    assert "未获人工批准" in res["reason"]
    assert ds.refund_records() == []


def test_executor_refuses_denied(env):
    """被驳回的动作不可执行。"""
    ds, gate, ex = env
    order = _refundable_order(ds)
    act = Action(ActionType.REFUND, {"amount": 1.0, "order_id": order.order_id}, RiskLevel.HIGH)
    _, req, _ = gate.check(act, "T-2")
    gate.deny(req.req_id, "主管", "证据不足")

    res = ex.execute(req)
    assert res["success"] is False
    assert ds.refund_records() == []


# ----------------------------------------------------------------------
# 真实落地
# ----------------------------------------------------------------------
def test_refund_actually_lands(env):
    """批准后：退款流水 +1、订单状态变更、审计留痕 decision=executed。"""
    ds, gate, ex = env
    order = _refundable_order(ds)
    amount = float(order.amount)
    act = Action(ActionType.REFUND, {"amount": amount, "order_id": order.order_id}, RiskLevel.HIGH)
    _, req, _ = gate.check(act, "T-3")
    gate.approve(req.req_id, "李运营")

    res = ex.execute(req)
    assert res["success"] is True, res
    assert ds.refund_records()[-1]["amount"] == amount
    assert ds.get_order(order.order_id).status == "退款中"
    assert gate.audit_dicts()[-1]["decision"] == "executed"
    assert req.status == "executed"


def test_reissue_deducts_inventory(env):
    """批准补发后库存真实扣减（原实现库存不变）。"""
    ds, gate, ex = env
    order, sku = _single_sku_with_stock(ds, need=2)
    if order is None:
        pytest.skip("数据集中没有单 SKU 且库存充足的订单")
    before = ds.get_inventory(order.store, sku)["available"]

    act = Action(ActionType.REISSUE,
                 {"qty": 1, "order_id": order.order_id, "sku": sku}, RiskLevel.HIGH)
    ok, req, msg = gate.check(act, "T-4")
    assert ok, msg
    gate.approve(req.req_id, "李运营")

    res = ex.execute(req)
    assert res["success"] is True, res
    after = ds.get_inventory(order.store, sku)["available"]
    assert after == before - 1


# ----------------------------------------------------------------------
# 幂等
# ----------------------------------------------------------------------
def test_executor_is_idempotent(env):
    """同一动作重复执行 -> 第二次被幂等拦截，不产生第二笔退款。"""
    ds, gate, ex = env
    order = _refundable_order(ds)
    act = Action(ActionType.REFUND, {"amount": 1.0, "order_id": order.order_id}, RiskLevel.HIGH)
    _, req, _ = gate.check(act, "T-5")
    gate.approve(req.req_id, "李运营")

    first = ex.execute(req)
    second = ex.execute(req)
    assert first["success"] is True
    assert second["success"] is False
    assert len(ds.refund_records()) == 1


def test_idem_key_composition():
    """幂等键由 (工单, 动作类型, 订单, SKU) 构成，同工单重复动作可被识别。"""
    from src.safety.gate import ApprovalRequest

    act = Action(ActionType.REFUND, {"amount": 1.0, "order_id": "PO-X"}, RiskLevel.HIGH)
    k_same_1 = ActionExecutor.idem_key(ApprovalRequest(action=act, ticket_id="T1"))
    k_same_2 = ActionExecutor.idem_key(ApprovalRequest(action=act, ticket_id="T1"))
    k_other_ticket = ActionExecutor.idem_key(ApprovalRequest(action=act, ticket_id="T2"))
    assert k_same_1 == k_same_2
    assert k_same_1 != k_other_ticket
    assert "refund" in k_same_1 and "PO-X" in k_same_1


# ----------------------------------------------------------------------
# 执行前复核
# ----------------------------------------------------------------------
def test_recheck_blocks_when_inventory_drained(env):
    """审批期间库存被占用 -> 执行前复核拦截，不产生超卖。"""
    ds, gate, ex = env
    order, sku = _single_sku_with_stock(ds, need=2)
    if order is None:
        pytest.skip("数据集中没有单 SKU 且库存充足的订单")

    act = Action(ActionType.REISSUE,
                 {"qty": 1, "order_id": order.order_id, "sku": sku}, RiskLevel.HIGH)
    _, req, _ = gate.check(act, "T-7")
    gate.approve(req.req_id, "李运营")

    # 模拟：审批期间该 SKU 库存被别的单吃掉
    inv = ds.get_inventory(order.store, sku)
    ds.deduct_inventory(order.store, sku, inv["available"])

    res = ex.execute(req)
    assert res["success"] is False
    assert "复核不通过" in res["reason"]


def test_approve_and_execute_helper(env):
    """一步式接口：批准并执行。"""
    ds, gate, ex = env
    order = _refundable_order(ds)
    act = Action(ActionType.REFUND, {"amount": 1.0, "order_id": order.order_id}, RiskLevel.HIGH)
    _, req, _ = gate.check(act, "T-8")

    res = ex.approve_and_execute(req.req_id, "李运营")
    assert res["success"] is True, res
    assert len(ds.refund_records()) == 1
