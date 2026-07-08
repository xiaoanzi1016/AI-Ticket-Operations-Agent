# -*- coding: utf-8 -*-
"""领域模型。

这些 dataclass 定义整个系统的核心数据结构：
- Order:  一笔订单（查询用）
- Return: 一笔退货/售后申请（查询用）
- Ticket: 一条售后/工单（问题载体）
- Action: 一个待执行动作（含风险等级，安全闸门据此拦截）
- Suggestion: Agent 给出的处理建议
- AuditRecord: 留痕记录（谁批的、何时、依据）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ActionType(str, Enum):
    """敏感/普通动作类型。"""

    QUERY = "query"                  # 只读查询
    SUGGEST = "suggest"              # 给建议
    REFUND = "refund"                # 退款（敏感）
    REISSUE = "reissue"              # 补发（敏感）
    MODIFY_ORDER = "modify_order"    # 改单（敏感）
    VOID_TICKET = "void_ticket"      # 作废工单（敏感）
    ESCALATE = "escalate"            # 升级人工


class RiskLevel(str, Enum):
    """动作风险等级，决定是否需要人工确认。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"          # 必须人工二次确认


# 敏感动作 -> 风险等级 映射（代码级硬编码，不依赖 LLM）
ACTION_RISK: dict[ActionType, RiskLevel] = {
    ActionType.QUERY: RiskLevel.LOW,
    ActionType.SUGGEST: RiskLevel.LOW,
    ActionType.REFUND: RiskLevel.HIGH,
    ActionType.REISSUE: RiskLevel.HIGH,
    ActionType.MODIFY_ORDER: RiskLevel.HIGH,
    ActionType.VOID_TICKET: RiskLevel.HIGH,
    ActionType.ESCALATE: RiskLevel.MEDIUM,
}


@dataclass
class Order:
    """一笔订单。"""

    order_id: str
    customer: str
    store: str
    amount: float
    status: str
    items: list[dict] = field(default_factory=list)   # [{sku, name, qty, price}]
    created_at: str = ""


@dataclass
class Return:
    """一笔退货/售后申请。

    大白话：客户把东西退回来了，记一笔"谁、哪个原订单、哪个商品、为什么退、退了多少钱"。

    为什么要有这个模型：退货数据（data/mock/returns.csv）原先只是躺在磁盘上，
    Agent 没有任何入口去查它。迁库后新增的 `get_returns` / `get_customer_return_rate`
    需要把查询结果交回上层，用它当作统一的返回类型 —— 和 `Order` 一样，
    领域层只依赖这个与存储无关的 dataclass，不把 ORM 对象泄漏出去。
    """

    order_id: str                      # 原订单号
    customer: str = ""
    store: str = ""
    sku: str = ""
    reason: str = ""
    amount: float = 0.0
    created_at: str = ""               # 申请时间，字符串形式（与 Order.created_at 口径一致）


@dataclass
class Ticket:
    """一条售后/工单。"""

    ticket_id: str
    customer: str                     # 消费者（服务对象）
    store: str                        # 所属门店
    issue_type: str                   # 少发/错发/破损/退款/投诉...
    description: str                  # 问题描述
    related_order_id: Optional[str] = None
    status: str = "open"              # open / processing / resolved / escalated
    created_at: str = ""


@dataclass
class Action:
    """一个待执行/已执行动作。"""

    type: ActionType
    params: dict                      # 如 {"amount": 458, "qty": 1, "order_id": "..."}
    risk: RiskLevel
    reason: str = ""                  # 为什么这样处理（留痕用）
    requires_approval: bool = False
    approved_by: Optional[str] = None   # 人工确认者


@dataclass
class Suggestion:
    """Agent 给出的处理建议（仅供展示，不执行）。

    outcome: RAG 增强引入。记录"这条建议后来怎么样了"（成功/被拦/转人工），
             用于两处：
             1. 沉淀进向量库时拼进案例文档 —— "结果"是案例叙事的关键一段；
             2. 前端展示时可以说明"这条建议当时的实际结局"。
             默认为空 dict：绝大多数调用点（规则版建议、单测）不关心它，
             加默认值保证旧调用完全不改。
    """

    summary: str
    actions: list[Action] = field(default_factory=list)
    escalation_required: bool = False
    escalation_reason: str = ""
    outcome: dict = field(default_factory=dict)


@dataclass
class AuditRecord:
    """留痕记录。"""

    ticket_id: str
    action: Action
    operator: str                     # 自动 / 人工ID
    decision: str                     # allowed / denied / escalated / executed
    timestamp: str
    note: str = ""


@dataclass
class GateOutcome:
    """安全闸门对**单个动作**的最终判定结果（评测层断言的唯一依据）。

    为什么需要它：`Action.requires_approval` 是由 ACTION_RISK 推导出的常量，
    任何实现都恒为 True，拿它做评测等于断言一个常量（指标恒真、无区分度）。
    GateOutcome 记录的是"闸门实际做了什么"，所以能真实区分好实现与坏实现：

    - allowed=False              -> 闸门拒绝（参数非法 / 订单不存在 / 超订单金额 …）
    - allowed=True, 需人工确认   -> 放行进二次确认（正确路径）
    - allowed=True, 无需确认     -> 只读动作自动执行
    - blocked_by="inventory"     -> 库存闸门额外拦截（保守转人工）
    """

    action_type: str
    allowed: bool
    requires_approval: bool
    blocked_by: str = ""              # "" / "gate" / "inventory"
    reason: str = ""
    executed: bool = False
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "action_type": self.action_type,
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
            "executed": self.executed,
            "params": self.params,
        }
