# -*- coding: utf-8 -*-
"""安全闸门 —— 本项目的核心差异化。

原则（与立项方案第 6 节一致）：
1. 敏感动作白名单：只有代码显式允许的动作才能执行。
2. 参数校验：金额/数量做**格式 + 范围 + 业务相对约束**三重校验。
3. 二次确认：HIGH 风险动作必须人工确认后才能真实执行。
4. 失败默认拒绝：任何校验异常/超时/信息缺失 → 拒绝并升级人工。

关键：安全不依赖大模型自觉，全部由代码强制。

加固记录（本轮）：
- 补「业务相对约束」：退款金额不得超过订单实付金额、订单必须存在且状态允许。
  原实现只校验绝对上限（5000），导致 15 元的订单可以申请退款 4999 元。
- 参数解析宽容化：LLM 常见的 `{"qty": "3"}` 字符串数字不再误判为"格式错误"。
- 别名冲突明确拒绝：`{"qty": 1, "quantity": 9999}` 不再静默取其一生效。
- 待确认请求改用稳定编号 req_id（原用 id()，是内存地址，重启即变且有复用风险）。
- 待确认/审计带 session_id 维度，支持按会话隔离。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional

from src.config import settings
from src.domain.models import ACTION_RISK, Action, ActionType, AuditRecord, RiskLevel
from src.logger import log

# 危险操作名单（非白名单动作一律拒绝）
_SAFE_AUTO_ACTIONS = {ActionType.QUERY, ActionType.SUGGEST, ActionType.ESCALATE}
# 敏感动作（HIGH）必须人工确认
_SENSITIVE_ACTIONS = {t for t, r in ACTION_RISK.items() if r == RiskLevel.HIGH}

# 内存待办队列里"已终结"（approved / denied / executed）条目的保留上限。
# 为什么需要它：原实现 `_pending` 只 append、从不移除，长跑的服务会一直堆，
# 而且 get_request() 是线性扫描，列表越长越慢。
# 为什么不是"一终结就移除"：刚批准的请求常常还要按 req_id 回查
# （mark_executed、界面回显），立刻删掉会出现"刚批准完就查不到"。
# 所以只在水位超过上限时批量归档 —— 见 SafetyGate._archive_terminal。
_MAX_TERMINAL_PENDING: int = 500

# 业务相对约束：订单状态必须处于可退款 / 可补发状态
# （已取消、待付款、退款中 都不接受新的退款/补发）
_REFUNDABLE_STATUSES = {"已付款待发货", "已发货", "已完成"}
_REISSUABLE_STATUSES = {"已付款待发货", "已发货", "已完成"}


@dataclass
class ApprovalRequest:
    """二次确认请求：敏感动作生成后进入待确认队列。"""

    action: Action
    ticket_id: str
    req_id: str = ""                 # 稳定编号（AP-0001），替代 id()
    session_id: str = ""             # 所属会话（多租户隔离用）
    reason: str = ""
    status: str = "pending"          # pending / approved / denied / executed
    note: str = ""
    executed_at: str = ""


class SafetyGate:
    """安全闸门：统一校验 + 二次确认管理。"""

    def __init__(self, order_lookup: Optional[Callable[[str], object]] = None) -> None:
        """
        order_lookup: 订单查询函数（注入式，便于单测与解耦）。
                      不传则懒加载全局 data_source.get_order。
        """
        self._pending: list[ApprovalRequest] = []
        self._audit: list[AuditRecord] = []
        self._seq = 0
        self._order_lookup = order_lookup

    # ------------------------------------------------------------------
    # 依赖注入
    # ------------------------------------------------------------------
    def _lookup_order(self, order_id: str):
        if self._order_lookup is not None:
            return self._order_lookup(order_id)
        from src.data_source import data_source  # 懒导入，避免循环依赖

        return data_source.get_order(order_id)

    # ------------------------------------------------------------------
    # 动作白名单
    # ------------------------------------------------------------------
    def is_action_allowed(self, action: Action) -> bool:
        """动作是否在白名单自动处理范围内（不含敏感动作）。"""
        return action.type in _SAFE_AUTO_ACTIONS

    # ------------------------------------------------------------------
    # 参数规范化 / 宽容解析
    # ------------------------------------------------------------------
    _ALIAS_MAP = {
        "quantity": "qty",
        "count": "qty",
        "num": "qty",
        "number": "qty",
        "money": "amount",
        "price": "amount",
        "refund_amount": "amount",
        "total": "amount",
        "orderid": "order_id",
        "order_no": "order_id",
        "orderno": "order_id",
        "order": "order_id",
        "product_code": "sku",
        "store_name": "store",
    }

    @staticmethod
    def _normalize_params(p: dict) -> dict:
        """参数规范化：统一 LLM 可能输出的字段别名。"""
        if not isinstance(p, dict):
            return {}
        out = dict(p)
        for alias, canonical in SafetyGate._ALIAS_MAP.items():
            if alias in out and canonical not in out:
                out[canonical] = out[alias]
        return out

    @staticmethod
    def _alias_conflict(p: dict) -> Optional[str]:
        """同义字段同时出现且值不一致 -> 返回冲突说明（调用方应拒绝）。

        原实现是"规范名优先、别名静默忽略"，语义含糊：`{"qty":1,"quantity":9999}`
        会取 1 生效。这里改为明确拒绝，不替调用方猜意图。
        """
        if not isinstance(p, dict):
            return None
        for alias, canonical in SafetyGate._ALIAS_MAP.items():
            if alias in p and canonical in p and p[alias] != p[canonical]:
                return f"参数冲突：{alias}={p[alias]!r} 与 {canonical}={p[canonical]!r} 不一致"
        return None

    @staticmethod
    def _to_number(v) -> Optional[float]:
        """宽容解析数字：LLM 常把数字输出成字符串（"3" / "3.0"）。"""
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            try:
                f = float(s)
            except ValueError:
                return None
            return int(f) if f.is_integer() else f
        return None

    # ------------------------------------------------------------------
    # 参数校验
    # ------------------------------------------------------------------
    def validate(self, action: Action) -> tuple[bool, str]:
        """参数合法性校验：格式 -> 范围 -> 业务相对约束。失败默认拒绝。"""
        raw = action.params or {}
        conflict = self._alias_conflict(raw)
        if conflict:
            return False, conflict
        p = self._normalize_params(raw)

        if action.type == ActionType.REFUND:
            return self._validate_refund(p)
        if action.type == ActionType.REISSUE:
            return self._validate_reissue(p)
        if action.type == ActionType.MODIFY_ORDER:
            order_id = str(p.get("order_id") or "").strip()
            if not order_id:
                return False, "订单号不能为空"
            if self._lookup_order(order_id) is None:
                return False, f"订单 {order_id} 不存在，拒绝改单"
        return True, "ok"

    def _validate_refund(self, p: dict) -> tuple[bool, str]:
        amount = self._to_number(p.get("amount"))
        if amount is None:
            return False, "退款金额缺失或格式错误"
        if amount <= 0:
            return False, "退款金额必须为正数"
        if amount > settings.refund_max_amount:
            return False, f"退款金额超过上限 {settings.refund_max_amount}，需人工介入"

        order_id = str(p.get("order_id") or "").strip()
        if not order_id:
            return False, "订单号不能为空"
        order = self._lookup_order(order_id)
        if order is None:
            return False, f"订单 {order_id} 不存在，拒绝退款"
        # 相对约束：退款不得超过订单实付金额（原实现缺失，属真实资金风险）
        order_amount = float(getattr(order, "amount", 0) or 0)
        if amount > order_amount:
            return False, f"退款金额 {amount} 超过订单实付金额 {order_amount}，拒绝"
        status = str(getattr(order, "status", "") or "")
        if status and status not in _REFUNDABLE_STATUSES:
            return False, f"订单状态「{status}」不支持退款"
        return True, "ok"

    def _validate_reissue(self, p: dict) -> tuple[bool, str]:
        qty = self._to_number(p.get("qty"))
        if qty is None:
            return False, "补发数量缺失或格式错误"
        if qty <= 0:
            return False, "补发数量必须为正数"
        if qty > settings.reissue_max_qty:
            return False, f"补发数量超过上限 {settings.reissue_max_qty}，需人工介入"

        order_id = str(p.get("order_id") or "").strip()
        if not order_id:
            return False, "订单号不能为空"
        order = self._lookup_order(order_id)
        if order is None:
            return False, f"订单 {order_id} 不存在，拒绝补发"
        status = str(getattr(order, "status", "") or "")
        if status and status not in _REISSUABLE_STATUSES:
            return False, f"订单状态「{status}」不支持补发"
        return True, "ok"

    # ------------------------------------------------------------------
    # 核心执行流程
    # ------------------------------------------------------------------
    def check(self, action: Action, ticket_id: str, session_id: str = ""
              ) -> tuple[bool, Optional[ApprovalRequest], str]:
        """对动作做完整安全校验。

        返回 (是否放行, 待确认请求, 说明)。
        - 放行=True 且 approval 非空：动作进入二次确认等待（还不能执行）。
        - 放行=True 且 approval 为空：动作可直接执行（只读/建议）。
        - 放行=False：动作被拒绝，说明原因。
        """
        # 1) 动作类型合法性
        if not action.type:
            return False, None, "动作类型为空，拒绝执行"

        # 2) 白名单
        if action.type not in ACTION_RISK:
            return False, None, f"动作 {action.type} 不在白名单，拒绝执行"

        # 3) 参数校验（格式 -> 范围 -> 业务相对约束）
        ok, msg = self.validate(action)
        if not ok:
            return False, None, f"参数校验失败：{msg}"

        # 4) 敏感动作 → 二次确认
        if action.type in _SENSITIVE_ACTIONS:
            req = ApprovalRequest(
                action=action,
                ticket_id=ticket_id,
                req_id=self._next_req_id(),
                session_id=session_id,
                reason=action.reason,
            )
            self._pending.append(req)
            # 水位过高时把已终结的条目归档掉，避免内存与扫描成本无限增长
            self._archive_terminal()
            log.info("敏感动作进入二次确认: %s %s ticket=%s", req.req_id, action.type, ticket_id)
            return True, req, "动作已进入人工二次确认，等待审批"

        # 5) 普通可执行动作
        return True, None, "动作通过校验，可执行"

    def _next_req_id(self) -> str:
        self._seq += 1
        return f"AP-{self._seq:04d}"

    # ------------------------------------------------------------------
    # 二次确认管理（人工操作）
    # ------------------------------------------------------------------
    def get_request(self, req_id: str) -> Optional[ApprovalRequest]:
        rid = str(req_id).strip()
        for r in self._pending:
            if r.req_id == rid:
                return r
        return None

    def list_pending(self, session_id: str | None = None) -> list[ApprovalRequest]:
        return [r for r in self._pending
                if r.status == "pending"
                and (session_id is None or r.session_id == session_id)]

    def _archive_terminal(self) -> int:
        """把已终结（非 pending）的确认请求移出内存待办队列，返回移除条数。

        注意：清掉的只是"待办队列"里的历史条目。
        **审计留痕（self._audit）是另一份数据，一条都不会少** ——
        审计要回答的是"谁批的、批了什么"，那部分从第一次判定起就写进了 _audit。

        已知限制（MVP）：_pending 仍在内存里，服务重启会丢失未处理的待确认项，
        且 `_seq` 归零会让 AP-0001 重新出现。要彻底解决需要把这层落到数据库
        （接口已按依赖注入设计，替换实现不影响调用方）。
        """
        if len(self._pending) <= _MAX_TERMINAL_PENDING:
            return 0
        before = len(self._pending)
        self._pending = [r for r in self._pending if r.status == "pending"]
        removed = before - len(self._pending)
        if removed:
            log.info("闸门：已归档 %d 条已终结的确认请求（待处理还剩 %d 条）",
                     removed, len(self._pending))
        return removed

    def approve(self, req_id: str, operator: str,
                session_id: str | None = None) -> tuple[bool, str]:
        """人工批准敏感动作执行（注意：批准 ≠ 已执行，执行由 ActionExecutor 负责）。

        session_id: 可选。传入时会校验该请求确实属于这个会话 ——
                    多租户场景下"光知道 req_id 就能批准别人的退款"是不能接受的。
                    不传时保持原有行为（单会话与内部调用兼容）。
        """
        r = self.get_request(req_id)
        if r is None or r.status != "pending":
            return False, "确认请求不存在或已处理"
        if session_id is not None and r.session_id != session_id:
            log.warning("跨会话批准被拒绝: req=%s 属于会话 %r，操作方声称 %r",
                        req_id, r.session_id, session_id)
            return False, f"确认请求 {req_id} 不属于会话 {session_id}，拒绝批准"
        r.status = "approved"
        r.note = f"批准人: {operator}"
        self._audit.append(
            AuditRecord(ticket_id=r.ticket_id, action=r.action, operator=operator,
                        decision="approved", timestamp=_now(), note="人工二次确认通过")
        )
        log.info("人工批准: %s %s by %s", r.req_id, r.action.type, operator)
        return True, "已批准"

    def deny(self, req_id: str, operator: str, reason: str,
             session_id: str | None = None) -> tuple[bool, str]:
        """人工驳回敏感动作（session_id 语义同 approve）。"""
        r = self.get_request(req_id)
        if r is None or r.status != "pending":
            return False, "确认请求不存在或已处理"
        if session_id is not None and r.session_id != session_id:
            log.warning("跨会话驳回被拒绝: req=%s 属于会话 %r，操作方声称 %r",
                        req_id, r.session_id, session_id)
            return False, f"确认请求 {req_id} 不属于会话 {session_id}，拒绝驳回"
        r.status = "denied"
        r.note = f"驳回人: {operator}, 原因: {reason}"
        self._audit.append(
            AuditRecord(ticket_id=r.ticket_id, action=r.action, operator=operator,
                        decision="denied", timestamp=_now(), note=f"人工驳回: {reason}")
        )
        log.info("人工驳回: %s %s by %s reason=%s", r.req_id, r.action.type, operator, reason)
        return True, "已驳回"

    def mark_executed(self, req_id: str, operator: str, note: str = "") -> bool:
        """标记请求已真实执行（由执行器调用）。"""
        r = self.get_request(req_id)
        if r is None:
            return False
        r.status = "executed"
        r.executed_at = _now()
        self._audit.append(
            AuditRecord(ticket_id=r.ticket_id, action=r.action, operator=operator,
                        decision="executed", timestamp=r.executed_at,
                        note=note or "动作已真实执行")
        )
        return True

    # ------------------------------------------------------------------
    def audit_log(self) -> list[AuditRecord]:
        return list(self._audit)

    # ------------------------------------------------------------------
    # 序列化（供界面/导出/评测使用）
    # ------------------------------------------------------------------
    def pending_dicts(self, session_id: str | None = None) -> list[dict]:
        """待确认请求 -> 可序列化 dict（含稳定编号 req_id）。"""
        return [
            {
                "id": r.req_id,
                "action": r.action.type.value,
                "params": r.action.params,
                "risk": r.action.risk.value,
                "ticket_id": r.ticket_id,
                "session_id": r.session_id,
                "reason": r.reason,
                "status": r.status,
            }
            for r in self._pending
            if r.status == "pending"
            and (session_id is None or r.session_id == session_id)
        ]

    def audit_dicts(self) -> list[dict]:
        """审计留痕 -> 可序列化 dict 列表（导出 JSON/CSV 用）。"""
        return [
            {
                "ticket_id": r.ticket_id,
                "action": r.action.type.value,
                "params": json.dumps(r.action.params, ensure_ascii=False),
                "operator": r.operator,
                "decision": r.decision,
                "timestamp": r.timestamp,
                "note": r.note,
            }
            for r in self._audit
        ]


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 全局唯一闸门实例（MVP 用单实例；生产可换 Redis 持久化）
safety_gate = SafetyGate()
