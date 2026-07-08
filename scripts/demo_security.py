# -*- coding: utf-8 -*-
"""安全加固演示：参数名规范化 + 库存闸门。

用法：python scripts/demo_security.py
不依赖 API key（纯安全层 + 数据源验证）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.domain.models import Action, ActionType, RiskLevel  # noqa: E402
from src.safety.gate import SafetyGate  # noqa: E402
from src.agent import create_agent  # noqa: E402
from src.data_source import DataSource  # noqa: E402

print("=" * 66)
print("场景 1：LLM 参数别名攻击 —— quantity 不再绕过数量上限")
print("=" * 66)
gate = SafetyGate()

# 数据源（后续场景共用）
ds = DataSource()
ds.load()

# LLM 用别名 quantity 输出一个超大补发数量（原本 qty 校验会失效）
attack = Action(ActionType.REISSUE, {"quantity": 9999, "order_id": "PO1"}, RiskLevel.HIGH)
ok, req, msg = gate.check(attack, "T1")
print(f"check(reissue, quantity=9999): 放行={ok} 说明={msg}")
# 规范化后 quantity->qty，qty=9999 > 上限 10 -> 应被拦
assert ok is False, "参数别名（quantity）应被规范化后拦截"
print("✅ 参数别名被规范化，超限补发被拦截")

print()
print("=" * 66)
print("场景 1b：count 别名同样生效")
print("=" * 66)
attack2 = Action(ActionType.REISSUE, {"count": 9999, "order_id": "PO1"}, RiskLevel.HIGH)
ok2, _, msg2 = gate.check(attack2, "T2")
assert ok2 is False
print(f"check(reissue, count=9999): 放行={ok2} 说明={msg2} -> 被拦截 ✅")

print()
print("=" * 66)
print("场景 1c：别名 money/price -> amount 统一退款金额校验")
print("=" * 66)
# money=99999 超上限，规范化后应被拦
attack3 = Action(ActionType.REFUND, {"money": 99999, "order_id": "PO1"}, RiskLevel.HIGH)
ok3, _, msg3 = gate.check(attack3, "T3")
print(f"check(refund, money=99999): 放行={ok3} 说明={msg3}")
assert ok3 is False
print("✅ 金额别名被规范化，超限退款被拦截")

# 缺失关键参数 -> 直接拒绝（之前 404 会误报"格式错误"）
attack4 = Action(ActionType.REFUND, {"note": "没有金额"}, RiskLevel.HIGH)
ok4, _, msg4 = gate.check(attack4, "T4")
print(f"check(refund, 缺金额): 放行={ok4} 说明={msg4}")
assert ok4 is False and "缺失" in msg4
print("✅ 关键参数缺失被明确拒绝")

print()
print("=" * 66)
print("场景 1d：业务相对约束 —— 退款不得超过订单实付金额（本轮新增）")
print("=" * 66)
refundable = [o for o in ds.all_orders()
              if o.amount > 0 and o.status in ("已付款待发货", "已发货", "已完成")]
small = min(refundable, key=lambda x: x.amount)
print(f"选取金额最小的可退款订单: {small.order_id} = {small.amount} 元（状态 {small.status}）")

over = Action(ActionType.REFUND, {"amount": 4999.0, "order_id": small.order_id}, RiskLevel.HIGH)
ok5, _, msg5 = gate.check(over, "T5")
print(f"check(refund 4999 元 / 订单实付 {small.amount} 元): 放行={ok5} 说明={msg5}")
assert ok5 is False and "超过订单实付" in msg5, "超额退款应被相对约束拦截"
print("✅ 超额退款被拦截（旧实现只校验绝对上限 5000，会放行 4999 元）")

normal = Action(ActionType.REFUND,
                {"amount": small.amount, "order_id": small.order_id}, RiskLevel.HIGH)
ok6, req6, msg6 = gate.check(normal, "T6")
print(f"check(refund 等于订单实付): 放行={ok6} 待确认={req6 is not None} 说明={msg6}")
assert ok6 is True and req6 is not None
print("✅ 正常金额通过校验并进入人工二次确认")

# 订单不存在 -> 拒绝
ghost = Action(ActionType.REFUND, {"amount": 10, "order_id": "PO00000000-00000"}, RiskLevel.HIGH)
ok7, _, msg7 = gate.check(ghost, "T7")
print(f"check(refund 订单不存在): 放行={ok7} 说明={msg7}")
assert ok7 is False

# 参数宽容化：字符串数字不再误判
soft = Action(ActionType.REISSUE, {"qty": "1", "order_id": small.order_id}, RiskLevel.HIGH)
ok8, _, msg8 = gate.check(soft, "T8")
print(f"check(reissue qty='1' 字符串): 放行={ok8} 说明={msg8}")
assert ok8 is True, "字符串数字应被宽容解析"
print("✅ 字符串数字宽容解析 + 订单不存在拒绝")

# 别名冲突 -> 明确拒绝（不再静默取其一）
conflict = Action(ActionType.REISSUE, {"qty": 1, "quantity": 9999, "order_id": small.order_id},
                  RiskLevel.HIGH)
ok9, _, msg9 = gate.check(conflict, "T9")
print(f"check(别名冲突 qty=1 / quantity=9999): 放行={ok9} 说明={msg9}")
assert ok9 is False
print("✅ 别名冲突被明确拒绝（旧实现会取 qty=1 静默生效）")


print()
print("=" * 66)
print("场景 2：库存闸门 —— 补发前校验库存")
print("=" * 66)
# 必须挑「单 SKU 订单」，否则会被"无法确定补发 SKU"提前拦截，测不到数量比较
order = None
sku = None
for o in ds.all_orders():
    if o.status not in ("已付款待发货", "已发货", "已完成"):
        continue
    skus = {it["sku"] for it in o.items if it.get("sku")}
    if len(skus) == 1:
        cand = next(iter(skus))
        inv0 = ds.get_inventory(o.store, cand)
        if inv0 and inv0["available"] >= 1:
            order, sku = o, cand
            break
if order is None:
    order = ds.all_orders()[0]
    sku = order.items[0]["sku"]

store = order.store
inv = ds.get_inventory(store, sku)
print(f"目标: 订单={order.order_id} 门店={store} sku={sku} "
      f"可用库存={inv['available'] if inv else '无'}")

agent = create_agent(mock=True)

# 数量恰好超过可用库存 -> 应被库存闸门拦截
if inv and inv["available"] >= 1:
    over_qty = int(inv["available"]) + 1
    big = Action(ActionType.REISSUE,
                 {"qty": over_qty, "order_id": order.order_id, "sku": sku}, RiskLevel.HIGH)
    ok_inv, inv_msg = agent._inventory_gate(big, order)
    print(f"库存闸门(补发{over_qty} > 可用{inv['available']}): 通过={ok_inv} 说明={inv_msg}")
    assert ok_inv is False
    print("✅ 库存不足 -> 拦截补发（真库存数量比较，不是绝对上限）")

    small = Action(ActionType.REISSUE,
                   {"qty": 1, "order_id": order.order_id, "sku": sku}, RiskLevel.HIGH)
    ok_inv2, inv_msg2 = agent._inventory_gate(small, order)
    print(f"库存闸门(补发1): 通过={ok_inv2} 说明={inv_msg2}")
    assert ok_inv2 is True

# 多 SKU 订单：无法确定 SKU -> 保守转人工（正确行为）
multi = next((o for o in ds.all_orders() if len({it["sku"] for it in o.items}) > 1), None)
if multi:
    amb = Action(ActionType.REISSUE, {"qty": 1, "order_id": multi.order_id}, RiskLevel.HIGH)
    ok_amb, amb_msg = agent._inventory_gate(amb, multi)
    print(f"多 SKU 订单({multi.order_id})保守处理: 通过={ok_amb} 说明={amb_msg}")
    assert ok_amb is False
    print("✅ 无法确定 SKU -> 保守转人工，不擅自放行")

print()
print("=" * 66)
print("完整链路：真实 LLM 输出别名参数也不绕过闸门（走 mock 全流程）")
print("=" * 66)
out = agent.run(f"客户反映订单 {order.order_id} 少发了一瓶，要求补发。", customer=order.customer)
for s in out["suggestions"]:
    print("摘要:", s["summary"])
    for a in s["actions"]:
        inv_note = a["params"].get("inventory_note", "")
        print(f"  action: {a['type']} | params={a['params']}" + ("  [库存:" + inv_note + "]" if inv_note else ""))

print()
print("安全加固验证完成 ✅")