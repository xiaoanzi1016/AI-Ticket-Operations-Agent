# -*- coding: utf-8 -*-
"""退货风险（业务规则）测试。

覆盖需求里的四条验收场景 + 三条边界：

1. 高退货率客户           -> 升级人工（escalate=True，建议正文即风控结论）
2. 低退货率客户           -> 走正常建议流程（不升级）
3. 访客                   -> 不做退货检查（checked=False）
4. CSV 降级模式           -> 不崩溃，静默跳过
5. RETURN_RISK_ENABLED=False -> 所有客户都走正常流程
6. 退货"次数"规则         -> 率没达标但次数够，仍标记高风险
7. SKU 高退货率规则       -> 在建议里提示"该商品近期退货率较高"

【为什么不用仓库里的 data/agent_operations.db】
那是真实审计数据，跑风控测试会往里灌测试客户。这里统一用 tmp_path 造一个
**受控的小型业务库**：真实数据整体退货率高达 87%（2117/2432），根本造不出
"低退货率客户"这种反例，只有自己造数据才能把退货率精确做成想要的形状。

【构造手法】
替换 `src.data_source.data_source` 模块属性（与 src/api/dataset.py 替换数据源的
做法完全一致）。注意 `src/tools/business_tools` 在模块顶层 `from ... import data_source`
绑定了自己的名字，必须**一并替换**，否则 create_suggestion 会读到旧数据源。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

# 统计窗口是"近 30 天"，所以测试数据必须落在窗口内（往前 2 天最稳）。
_RECENT = datetime.now() - timedelta(days=2)

# 说明：退货表的 customer 故意写脱敏代号（"收货人99"），
# 与订单表的真实姓名毫无交集 —— 用来证明风控**不依赖**退货表的收货人字段
# （真实数据就是这样，见 crud.get_customer_return_rate 的说明）。
_DESENSITIZED = "收货人99"


def _order(order_id: str, customer: str, sku: str) -> dict:
    """造一条订单行（items 是 JSON 字符串，与 Order.items 的存储形态一致）。

    状态统一用"已完成"：它在安全闸门的可退款/可补发白名单里
    （见 src/safety/gate.py 的 _REFUNDABLE_STATUSES），
    这样测试关注的是"退货风险"，而不是被订单状态闸门先拦下来。
    """
    return {
        "order_id": order_id,
        "customer": customer,
        "store": "测试门店",
        "amount": 100.0,
        "status": "已完成",
        "items": json.dumps(
            [{"sku": sku, "name": "测试商品", "spec": "", "qty": 1, "price": 100.0}],
            ensure_ascii=False,
        ),
        "created_at": _RECENT,
    }


def _ret(order_id: str, sku: str) -> dict:
    """造一条退货行（收货人用脱敏代号，证明按订单关联才是对的口径）。"""
    return {
        "order_id": order_id,
        "customer": _DESENSITIZED,
        "store": "测试门店",
        "sku": sku,
        "reason": "质量问题",
        "amount": 100.0,
        "created_at": _RECENT,
    }


def _build_orders() -> list[dict]:
    """受控订单集（退货率是刻意设计出来的，注释里标了每类的预期）。

    商品 SKU 只有两个，方便精确控制"SKU 退货率"：
      - SKU-NORMAL: 21 单 / 5 单退过 -> 23.8%，**低于** 30% 阈值（不触发规则 3）
      - SKU-HOT   :  7 单 / 4 单退过 -> 57.1%，**高于** 30% 阈值（触发规则 3）
    """
    rows: list[dict] = []

    # 高频客户：4 单退 3 单 -> 75%，触发规则 1（升级人工）
    for i in range(1, 5):
        rows.append(_order(f"PO20260928-0000{i}", "高频客户", "SKU-NORMAL"))

    # 低频客户：4 单 0 退 -> 0%，规则 1/2/3 全不触发（正常流程）
    for i in range(11, 15):
        rows.append(_order(f"PO20260928-000{i}", "低频客户", "SKU-NORMAL"))

    # 中频客户：10 单退 2 单 -> 20%（未达 40%），但次数 2 >= 2 -> 只触发规则 2
    for i in range(21, 31):
        rows.append(_order(f"PO20260928-000{i}", "中频客户", "SKU-NORMAL"))

    # 单品客户：4 单 0 退 -> 本人无风险；但首单含 SKU-HOT -> 触发规则 3
    rows.append(_order("PO20260928-00041", "单品客户", "SKU-HOT"))
    for i in range(42, 45):
        rows.append(_order(f"PO20260928-000{i}", "单品客户", "SKU-NORMAL"))

    # 其他客户：给 SKU-HOT 贡献"高退货率"的样本（这些单子不属于被测客户）
    for i in range(51, 57):
        rows.append(_order(f"PO20260928-000{i}", f"其他客户{i}", "SKU-HOT"))

    return rows


def _build_returns() -> list[dict]:
    """受控退货集（与 _build_orders 一一对应）。"""
    rows: list[dict] = []
    # 高频客户退 3 单
    for i in (1, 2, 3):
        rows.append(_ret(f"PO20260928-0000{i}", "SKU-NORMAL"))
    # 中频客户退 2 单
    for i in (21, 22):
        rows.append(_ret(f"PO20260928-000{i}", "SKU-NORMAL"))
    # SKU-HOT 退 4 单（都是"其他客户"的单，却把 SKU 的退货率抬高了）
    for i in (51, 52, 53, 54):
        rows.append(_ret(f"PO20260928-000{i}", "SKU-HOT"))
    return rows


@pytest.fixture()
def biz_datasource(tmp_path, monkeypatch):
    """建一个受控的小型业务库，并把全局数据源换成它的 DB 版。"""
    import src.data_source as ds_mod
    import src.tools.business_tools as bt
    from src.persistence import crud
    from src.persistence.database import build_engine, init_db, make_session_factory

    engine = build_engine(tmp_path / "biz.db")
    init_db(engine)
    factory = make_session_factory(engine)

    db = factory()
    try:
        crud.replace_orders(db, _build_orders())
        crud.replace_returns(db, _build_returns())
    finally:
        db.close()

    ds = ds_mod.DatabaseDataSource(session_factory=factory)
    # 两处都要换：agent 在函数内 `from src.data_source import data_source` 取模块属性，
    # 而 business_tools 在模块顶层做了同名绑定，不换就会读到真实库。
    monkeypatch.setattr(ds_mod, "data_source", ds)
    monkeypatch.setattr(bt, "data_source", ds)
    try:
        yield ds
    finally:
        engine.dispose()


def _agent():
    """建一个不落库的 mock Agent（测试只想看决策，不想写审计库）。"""
    from src.agent import create_agent

    return create_agent(mock=True, persistence=False)


# ----------------------------------------------------------------------
# 1) 高退货率客户 -> 升级人工
# ----------------------------------------------------------------------
def test_high_return_rate_customer_is_escalated(biz_datasource):
    agent = _agent()
    out = agent.run("客户反映订单 PO20260928-00001 要求退款。", customer="高频客户")

    risk = out["return_risk"]
    assert risk["checked"] is True
    assert risk["escalate"] is True
    assert risk["details"]["customer_rate"] == 0.75
    assert risk["details"]["return_count"] == 3
    assert risk["details"]["order_count"] == 4

    sug = out["suggestions"][0]
    # 建议正文就是风控结论 —— 回复里必须有"升级人工"字样（验收标准 2）
    assert "升级人工" in sug["summary"]
    assert "75%" in sug["summary"]
    assert sug["escalation_required"] is True
    assert "退货率" in sug["escalation_reason"]


def test_escalated_customer_still_goes_through_safety_gate(biz_datasource):
    """升级人工不等于跳过闸门：动作仍要过闸门、仍进待确认列表（可审计）。"""
    agent = _agent()
    out = agent.run("客户反映订单 PO20260928-00001 要求退款。", customer="高频客户")

    assert out["gate_outcomes"], "升级路径也必须留下闸门判定"
    assert out["pending_approvals"], "敏感动作应进入人工二次确认"
    # 高风险动作被强制要求人工确认
    for act in out["suggestions"][0]["actions"]:
        assert act["requires_approval"] is True


# ----------------------------------------------------------------------
# 2) 低退货率客户 -> 正常流程（验收标准 3）
# ----------------------------------------------------------------------
def test_low_return_rate_customer_takes_normal_flow(biz_datasource):
    agent = _agent()
    out = agent.run("客户反映订单 PO20260928-00011 少发了一瓶，要求补发。",
                    customer="低频客户")

    risk = out["return_risk"]
    assert risk["checked"] is True
    assert risk["escalate"] is False
    assert risk["high_risk"] is False
    assert risk["details"]["customer_rate"] == 0.0
    assert risk["details"]["return_count"] == 0

    sug = out["suggestions"][0]
    assert "升级人工" not in sug["summary"]          # 不该被误升级
    assert "退货风险提示" not in sug["summary"]
    # 走的是"按问题类型生成建议"的正常路径：少发 -> 补发动作
    assert [a["type"] for a in sug["actions"]] == ["reissue"]


# ----------------------------------------------------------------------
# 3) 访客 -> 不做退货检查（验收标准 4）
# ----------------------------------------------------------------------
def test_visitor_skips_return_check(biz_datasource):
    """用不存在的订单号复现"访客"：order 查不到 -> ticket.customer = '访客'。"""
    agent = _agent()
    out = agent.run("客户反映订单 PO20260928-99999 少发了一瓶，要求补发。",
                    customer="访客")

    risk = out["return_risk"]
    assert risk["checked"] is False
    assert risk["escalate"] is False
    assert risk["high_risk"] is False
    assert risk["details"] == {}
    # 正常出建议，没有被风控影响
    assert out["suggestions"]


@pytest.mark.parametrize("name", ["", "匿名", "unknown", "Anonymous"])
def test_anonymous_customer_names_all_skip(biz_datasource, name, monkeypatch):
    """各类"匿名"写法都不能触发风控（防止对着不认识的人空跑规则）。"""
    from src.domain.models import Ticket

    agent = _agent()
    risk = agent._check_return_risk(
        Ticket(ticket_id="T-X", customer=name, store="", issue_type="咨询",
               description="x"), None)
    assert risk["checked"] is False


# ----------------------------------------------------------------------
# 4) CSV 降级模式 -> 不崩溃（验收标准 4 的另一半）
# ----------------------------------------------------------------------
def test_csv_fallback_mode_skips_without_crash(monkeypatch):
    """数据源退化为 CSV 时，风控整体跳过，绝不能抛异常。"""
    import src.data_source as ds_mod

    monkeypatch.setattr(ds_mod, "data_source", ds_mod.DataSource())
    agent = _agent()
    out = agent.run("客户反映订单 PO20260928-00001 要求退款。", customer="任意客户")

    assert out["return_risk"]["checked"] is False
    assert out["suggestions"], "CSV 模式下仍要能正常出建议"


def test_check_return_risk_swallows_datasource_errors(monkeypatch):
    """数据源在查询过程中抛异常时，必须降级为"无风险"而不是把工单带崩。"""
    import src.data_source as ds_mod

    class _Boom(ds_mod.DatabaseDataSource):
        def all_orders(self):
            raise RuntimeError("模拟库连接断开")

    monkeypatch.setattr(ds_mod, "data_source", _Boom(session_factory=lambda: None))
    agent = _agent()
    from src.domain.models import Ticket

    out = agent._check_return_risk(
        Ticket(ticket_id="T-1", customer="任意", store="",
               issue_type="咨询", description="x"), None)
    assert out["checked"] is False
    assert out["escalate"] is False


# ----------------------------------------------------------------------
# 5) 总开关关闭（验收标准 5）
# ----------------------------------------------------------------------
def test_switch_off_disables_all_checks(biz_datasource, monkeypatch):
    """RETURN_RISK_ENABLED=False：连高退货率客户也走正常流程。"""
    agent = _agent()
    monkeypatch.setattr(agent, "return_risk_enabled", False)

    out = agent.run("客户反映订单 PO20260928-00001 要求退款。", customer="高频客户")
    assert out["return_risk"]["checked"] is False
    sug = out["suggestions"][0]
    assert "升级人工" not in sug["summary"]
    assert sug["escalation_required"] is False
    # 仍是正常的退款建议路径
    assert [a["type"] for a in sug["actions"]] == ["refund"]


def test_switch_is_off_when_env_says_false(monkeypatch):
    """配置层：RETURN_RISK_ENABLED=false 解析为布尔 False。"""
    import os

    monkeypatch.setenv("RETURN_RISK_ENABLED", "false")
    from src.config import Settings

    assert Settings().return_risk_enabled is False
    monkeypatch.setenv("RETURN_RISK_ENABLED", "true")
    assert Settings().return_risk_enabled is True
    assert os.getenv("RETURN_RISK_ENABLED") == "true"


# ----------------------------------------------------------------------
# 6) 退货"次数"规则（率未达标但次数够）
# ----------------------------------------------------------------------
def test_min_count_rule_marks_high_risk_without_escalating(biz_datasource):
    agent = _agent()
    out = agent.run("客户咨询订单 PO20260928-00021 的处理进度。", customer="中频客户")

    risk = out["return_risk"]
    assert risk["checked"] is True
    assert risk["details"]["customer_rate"] == 0.2      # 未达 40% -> 不升级
    assert risk["escalate"] is False
    assert risk["details"]["return_count"] == 2         # 达到 2 次 -> 高风险
    assert risk["high_risk"] is True
    assert "人工审核" in risk["reason"]


# ----------------------------------------------------------------------
# 7) SKU 高退货率规则
# ----------------------------------------------------------------------
def test_hot_sku_adds_note_to_suggestion(biz_datasource):
    """客户本身干净，但订单主商品退货率高 -> 建议里要带上验货提示。"""
    agent = _agent()
    out = agent.run("客户咨询订单 PO20260928-00041 的处理进度。", customer="单品客户")

    risk = out["return_risk"]
    assert risk["details"]["customer_rate"] == 0.0      # 客户维度干净
    assert risk["escalate"] is False
    assert risk["details"]["sku"] == "SKU-HOT"
    assert risk["details"]["sku_rate"] > 0.3
    assert risk["high_risk"] is True
    assert any("SKU-HOT" in n and "退货率" in n for n in risk["notes"])

    sug = out["suggestions"][0]
    assert "退货风险提示" in sug["summary"]
    assert "SKU-HOT" in sug["summary"]
    assert sug["escalation_required"] is True


def test_normal_sku_does_not_add_note(biz_datasource):
    """SKU-NORMAL 退货率 23.8% < 30%，不该加提示（避免噪声）。"""
    agent = _agent()
    out = agent.run("客户咨询订单 PO20260928-00011 的处理进度。", customer="低频客户")

    risk = out["return_risk"]
    assert risk["details"]["sku"] == "SKU-NORMAL"
    assert risk["details"]["sku_rate"] < 0.3
    assert risk["high_risk"] is False
    assert risk["notes"] == []


# ----------------------------------------------------------------------
# 8) 结果结构稳定性
# ----------------------------------------------------------------------
def test_return_risk_payload_has_stable_shape(biz_datasource):
    """返回体字段固定 —— 上层/前端可以无条件读取，不必到处判空。"""
    agent = _agent()
    out = agent.run("客户咨询订单 PO20260928-00011 的处理进度。", customer="低频客户")
    risk = out["return_risk"]
    assert set(risk) == {"checked", "escalate", "high_risk", "reason", "notes", "details"}
    assert isinstance(risk["notes"], list)
    assert isinstance(risk["details"], dict)
