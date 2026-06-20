# -*- coding: utf-8 -*-
"""执行层：把「已人工批准」的动作真正落成业务副作用。

为什么需要这一层：原实现里 `SafetyGate.approve()` 只改了内存状态位 + 写审计，
全仓库没有任何代码在批准后真的退款/发货 —— 也就是"有闸门、没有门后的执行"。
本模块补齐闭环，并解决随之而来的两个问题：

1. **幂等**：同一动作重复提交（重复点批准 / 消息重投）只执行一次。
   原实现不扣库存，同一批库存可被反复批准补发。
2. **执行前复核**：审批期间库存/订单状态可能已变，执行前必须再校验一次。

安全边界：执行器只接受 `status == "approved"` 的请求；其余一律拒绝。
"""
from __future__ import annotations

from typing import Any, Optional

from src.data_source import data_source as global_data_source
from src.domain.models import ActionType
from src.logger import log
from src.safety.gate import ApprovalRequest, SafetyGate, safety_gate


class ActionExecutor:
    """敏感动作执行器（人工批准后才可调用）。"""

    def __init__(self, gate: Optional[SafetyGate] = None, data_source=None) -> None:
        self.gate = gate or safety_gate
        self.ds = data_source or global_data_source
        self._executed: set[str] = set()

    # ------------------------------------------------------------------
    # 幂等键
    # ------------------------------------------------------------------
    @staticmethod
    def idem_key(req: ApprovalRequest) -> str:
        """(工单, 动作类型, 订单, SKU) 作为幂等键。"""
        p = req.action.params or {}
        return "|".join([
            str(req.ticket_id),
            req.action.type.value,
            str(p.get("order_id", "")),
            str(p.get("sku", "")),
        ])

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def execute(self, req: Optional[ApprovalRequest]) -> dict:
        """执行一个已批准的动作。返回 {success, reason, detail, ...}。"""
        if req is None:
            return {"success": False, "reason": "确认请求不存在"}

        # 1) 状态门：未获人工批准，一律拒绝（安全双保险）
        if req.status == "executed":
            return {"success": False, "reason": "该动作已执行，幂等拦截", "idempotent": True}
        if req.status != "approved":
            return {"success": False,
                    "reason": f"请求状态为 {req.status}，未获人工批准，拒绝执行"}

        # 2) 幂等门：同一动作只执行一次
        key = self.idem_key(req)
        if key in self._executed:
            return {"success": False, "reason": "重复请求，幂等拦截", "idempotent": True}

        # 3) 执行前复核（审批期间业务状态可能已变化）
        ok, msg = self._recheck(req)
        if not ok:
            return {"success": False, "reason": f"执行前复核不通过：{msg}"}

        # 4) 落业务
        atype = req.action.type
        handler = {
            ActionType.REFUND: self._do_refund,
            ActionType.REISSUE: self._do_reissue,
            ActionType.MODIFY_ORDER: self._do_modify_order,
            ActionType.VOID_TICKET: self._do_void_ticket,
        }.get(atype)
        if handler is None:
            return {"success": False, "reason": f"动作 {atype} 没有对应的执行器实现"}

        result = handler(req)
        if result.get("success"):
            self._executed.add(key)
            self.gate.mark_executed(req.req_id, "系统执行器", result.get("detail", ""))
            log.info("动作已执行: %s %s -> %s", req.req_id, atype, result.get("detail"))
        return result

    def approve_and_execute(self, req_id: str, operator: str) -> dict:
        """人工批准 + 立即执行（控制台/界面用的一步式接口）。"""
        req = self.gate.get_request(req_id)
        if req is None:
            return {"success": False, "reason": "确认请求不存在"}
        ok, msg = self.gate.approve(req_id, operator)
        if not ok:
            return {"success": False, "reason": msg}
        return self.execute(req)

    # ------------------------------------------------------------------
    # 执行前复核
    # ------------------------------------------------------------------
    def _recheck(self, req: ApprovalRequest) -> tuple[bool, str]:
        """复用安全闸门做一次全量复核（参数/订单/状态），并额外查库存。"""
        ok, msg = self.gate.validate(req.action)
        if not ok:
            return False, msg

        if req.action.type == ActionType.REISSUE:
            p = SafetyGate._normalize_params(req.action.params or {})
            order_id = str(p.get("order_id", "")).strip()
            qty = SafetyGate._to_number(p.get("qty")) or 0
            order = self.ds.get_order(order_id)
            if order is None:
                return False, f"订单 {order_id} 已不存在"
            sku = self._resolve_sku(p, order)
            if not sku:
                return False, "无法确定补发 SKU"
            inv = self.ds.get_inventory(order.store, sku)
            if inv is None:
                return False, f"门店 {order.store} 无商品 {sku} 库存记录"
            if inv["available"] < qty:
                return False, (f"门店 {order.store} 商品 {sku} 可用库存 "
                               f"{inv['available']} 不足（需 {qty}）")
        return True, "ok"

    @staticmethod
    def _resolve_sku(p: dict, order) -> str:
        sku = p.get("sku")
        if sku:
            return str(sku)
        unique = {it["sku"] for it in (order.items or []) if it.get("sku")}
        return unique.pop() if len(unique) == 1 else ""

    # ------------------------------------------------------------------
    # 各动作的落地实现
    # ------------------------------------------------------------------
    def _do_refund(self, req: ApprovalRequest) -> dict:
        p = SafetyGate._normalize_params(req.action.params or {})
        order_id = str(p.get("order_id", "")).strip()
        amount = float(SafetyGate._to_number(p.get("amount")) or 0)
        order = self.ds.get_order(order_id)
        if order is None:
            return {"success": False, "reason": f"订单 {order_id} 不存在"}
        self.ds.record_refund(order_id, amount, req.note or "系统执行器")
        self.ds.set_order_status(order_id, "退款中")
        return {
            "success": True,
            "detail": f"已对订单 {order_id} 退款 {amount} 元，订单状态置为「退款中」",
            "order_id": order_id,
            "amount": amount,
        }

    def _do_reissue(self, req: ApprovalRequest) -> dict:
        p = SafetyGate._normalize_params(req.action.params or {})
        order_id = str(p.get("order_id", "")).strip()
        qty = float(SafetyGate._to_number(p.get("qty")) or 0)
        order = self.ds.get_order(order_id)
        if order is None:
            return {"success": False, "reason": f"订单 {order_id} 不存在"}
        sku = self._resolve_sku(p, order)
        if not sku:
            return {"success": False, "reason": "无法确定补发 SKU（订单含多个商品）"}
        ok, msg = self.ds.deduct_inventory(order.store, sku, qty)
        if not ok:
            return {"success": False, "reason": msg}
        return {
            "success": True,
            "detail": f"已补发 {qty} 件 {sku}（门店 {order.store}），库存已扣减",
            "order_id": order_id,
            "sku": sku,
            "qty": qty,
        }

    def _do_modify_order(self, req: ApprovalRequest) -> dict:
        p = SafetyGate._normalize_params(req.action.params or {})
        return {"success": True,
                "detail": f"订单 {p.get('order_id')} 改单已受理并留痕（无外部系统对接）",
                "order_id": p.get("order_id")}

    def _do_void_ticket(self, req: ApprovalRequest) -> dict:
        return {"success": True,
                "detail": f"工单 {req.ticket_id} 作废已受理并留痕",
                "ticket_id": req.ticket_id}

    # ------------------------------------------------------------------
    def executed_keys(self) -> list[str]:
        """已执行的幂等键（供检查/测试）。"""
        return sorted(self._executed)


# 全局执行器（演示/CLI 用；生产建议按依赖注入创建）
executor = ActionExecutor()
