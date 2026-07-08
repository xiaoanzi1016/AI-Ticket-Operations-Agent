# -*- coding: utf-8 -*-
"""记忆落数据演示：会话记忆（按 session 分桶） + 跨会话用户画像。

用法：python scripts/demo_memory.py [--llm]
- 默认 mock 模式（无需 key，快速验证记忆链路）
- --llm 用真实模型（可看到画像注入对回复的影响）

演示内容：
1. 客户张三第一次工单 -> 画像记录门店/商品/问题类型
2. 再次找 Agent -> 画像摘要注入上下文（"该客户常处理..."）
3. 客户李四 -> **会话记忆与画像双重隔离**（不串人）

说明：原版只断言了长期画像隔离，会话记忆是全局单例（张三的对话会进李四的
上下文）。本轮改为按 session_id 分桶，并补上对**会话记忆隔离**的显式断言。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.agent import create_agent  # noqa: E402
from src.memory.store import SessionStore, user_profile  # noqa: E402


def show_profile(customer: str):
    print(f"  [画像] {customer}: {user_profile.summary_for(customer) or '(空)'}")


def main() -> None:
    use_llm = "--llm" in sys.argv
    store = SessionStore()
    user_profile.clear()

    agent = create_agent(mock=not use_llm, use_llm=use_llm, session_store=store)
    mode = "真实 LLM" if use_llm else "mock"
    print("=" * 66)
    print(f"记忆演示（{mode} 模式）")
    print("=" * 66)

    print("\n--- 第 1 次工单：张三（少发补发） ---")
    agent.run("客户张三反映订单 PO20260928-00001 少发了一瓶玻璃水，要求补发。", customer="张三")
    show_profile("张三")
    print(f"  张三会话历史条数: {len(store.get('cust:张三').messages())}")

    print("\n--- 第 2 次工单：张三（换商品查库存） ---")
    agent.run("客户张三想查订单 PO20260928-00002 的物流进度。", customer="张三")
    show_profile("张三")
    print(f"  张三会话历史条数: {len(store.get('cust:张三').messages())}")
    print(f"  会话摘要: {store.get('cust:张三').summarize()[:150]}")

    print("\n--- 第 3 次工单：李四（新客户） ---")
    agent.run("客户李四买的行车记录仪坏了要退款。", customer="李四")
    show_profile("李四")
    show_profile("张三")   # 张三画像不受影响
    print(f"  李四会话历史条数: {len(store.get('cust:李四').messages())}")

    print("\n" + "=" * 66)
    print("记忆链路验证")
    print("=" * 66)

    # ---- 长期画像隔离 ----
    zhang = user_profile.get("张三")
    li = user_profile.get("李四")
    assert zhang, "张三画像应存在"
    assert zhang["last_store"] == "苏州工业园店", "张三画像应记录门店"
    assert "李四" in user_profile._profiles, "李四画像应存在"

    # ---- 会话记忆隔离（原版缺失的关键断言） ----
    zhang_msgs = store.get("cust:张三").messages()
    li_msgs = store.get("cust:李四").messages()
    leaked = [m for m in li_msgs if "张三" in str(m.get("content", ""))]
    assert not leaked, f"会话记忆串台！李四上下文里出现了张三的内容: {leaked}"
    assert zhang_msgs, "张三会话记忆应非空"

    print(f"✅ 张三画像: 门店计数 {zhang['store_counter']} | SKU计数 {zhang['sku_counter']}"
          f" | 问题计数 {zhang['issue_counter']}")
    print(f"✅ 李四画像: {li['last_issue'] if li else '(空)'}（与张三完全独立）")
    print(f"✅ 会话隔离: 张三 {len(zhang_msgs)} 条 / 李四 {len(li_msgs)} 条，"
          f"无交叉（桶: {store.sessions()}）")
    print("\n记忆演示通过 ✅")


if __name__ == "__main__":
    main()
