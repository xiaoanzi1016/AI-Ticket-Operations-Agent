# -*- coding: utf-8 -*-
"""RAG 演示：历史工单案例的 FTS5 全文检索。

用法：python scripts/demo_fts.py [--keep] [--agent]
- 默认：往案例库写入 3 条演示案例，再跑 6 个查询看召回效果（不调 LLM）。
- --keep：保留已有案例，只追加演示案例（不清库）。
- --agent：额外跑一遍带 RAG 的真实 Agent 流程，展示"历史案例注入 prompt"。

【演示什么】
1. 写入 3 条历史工单（少发 / 破损 / 退款）。
2. 用不同说法查询，验证**中文 bigram 切词 + 同义词扩展**的召回效果：
   - 原话命中（"少发一瓶玻璃水"）
   - 口语说法命中（"东西碎了" -> "破损"案例）
   - 不相关查询不命中（避免噪声污染）
3. 打印每条命中的相似度（bm25 换算的 [0,1] 分值）。

【为什么不用向量库】
FTS5 是 SQLite 内建模块，零新依赖、零模型下载，离线开箱可用。
代价是"只按关键词命中、不做语义泛化" —— 本项目的工单文本用词高度重复，
这个权衡可以接受（详见 src/memory/fts_store.py 头部说明）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:  # Windows 控制台默认 GBK，打印中文前先切 UTF-8，避免 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover - 非 Windows / 老 Python 忽略
    pass

from src.memory.fts_store import FtsCaseStore, _display_width  # noqa: E402

KEEP = "--keep" in sys.argv
RUN_AGENT = "--agent" in sys.argv

# 3 条演示案例：覆盖少发 / 破损 / 退款三类高频问题，并带不同"结果"
DEMO_CASES = [
    {
        "ticket_id": "DEMO-T001",
        "description": "客户反馈订单少发了一瓶玻璃水，只收到 2 瓶。",
        "suggestion": "核实发货记录后为门店补发同款 1 瓶。",
        "outcome": "成功执行：已补发，客户确认收货。",
        "metadata": {"issue_type": "少发", "store": "苏州工业园店", "success": True},
    },
    {
        "ticket_id": "DEMO-T002",
        "description": "客户反馈收到的商品外包装破损，瓶身有裂纹。",
        "suggestion": "按破损流程更换同款商品，原货退回。",
        "outcome": "成功执行：已换货寄出。",
        "metadata": {"issue_type": "破损", "store": "苏州工业园店", "success": True},
    },
    {
        "ticket_id": "DEMO-T003",
        "description": "客户要求对已签收订单全额退款，金额超出单笔上限。",
        "suggestion": "建议金额超限，转人工审核后决定是否退款。",
        "outcome": "被拦截：单笔退款金额超过订单实付金额，需人工复核。",
        "metadata": {"issue_type": "退款", "store": "杭州西湖店", "success": False},
    },
]

# 6 个查询：既有期望命中的，也有期望"零命中"的（验证不引入噪声）
QUERIES = [
    ("少发一瓶玻璃水", "原话命中 -> 期望命中 少发案例"),
    ("东西碎了要退款", "口语说法 -> 期望命中 破损/退款案例"),
    ("商品坏了", "口语说法 -> 期望命中 破损案例"),
    ("想退货退钱", "口语说法 -> 期望命中 退款案例"),
    ("快递一直不到", "库里无对应案例 -> 期望 0~少量命中"),
    ("今天天气不错xyz", "完全无关 -> 期望 0 命中"),
]


def _pad(text: str, width: int) -> str:
    """按显示宽度右补空格（中文按 2 列算），保证表格对齐。"""
    return text + " " * max(0, width - _display_width(text))


def main() -> None:
    store = FtsCaseStore()
    print("=" * 70)
    print("RAG 演示 · 历史工单案例 FTS5 全文检索")
    print("=" * 70)
    health = store.health()
    print(f"案例库: {health['db_path']}")
    print(f"可用性: {'✅ 可用' if health['available'] else '❌ 不可用 ' + health['reason']}")
    if not health["available"]:
        print("\n当前 SQLite 不支持 FTS5 或库不可写，演示结束。")
        return

    if not KEEP:
        store.reset()
        print("已清空旧案例（--keep 可保留）。")
    for c in DEMO_CASES:
        store.add_case(c["ticket_id"], c["description"], c["suggestion"],
                       c["outcome"], c["metadata"])
    print(f"\n已写入 {len(DEMO_CASES)} 条演示案例，当前库中共 {store.count()} 条。")

    print("\n" + "-" * 70)
    print("检索验证")
    print("-" * 70)
    for q, expect in QUERIES:
        hits = store.search(q, top_k=3)
        print(f"\n查询「{q}」  ({expect})")
        if not hits:
            print("  -> 无命中")
            continue
        for h in hits:
            meta = h.get("metadata") or {}
            print(f"  -> {_pad(h['ticket_id'], 12)} 相似度 {h['score']:<7} "
                  f"类型 {meta.get('issue_type', '-')}")
            print(f"     {h['description'][:48]}")

    if RUN_AGENT:
        _demo_agent()

    print("\n" + "=" * 70)
    print("演示完成 ✅  （案例库文件与业务库同源：data/agent_operations.db）")
    print("=" * 70)


def _demo_agent() -> None:
    """跑一遍真实 Agent 流程，展示历史案例注入 system prompt。"""
    print("\n" + "-" * 70)
    print("Agent 端到端：RAG 案例注入（mock 模式，不调 LLM）")
    print("-" * 70)
    import os

    os.environ.setdefault("AGENT_FORCE_MOCK", "1")
    from src.agent import create_agent
    from src.memory.fts_store import get_fts_store

    agent = create_agent(mock=True, persistence=False)
    # 演示脚本与 Agent 共用全局单例，避免两条连接指向不同库文件
    if agent.fts_store.db_path != get_fts_store().db_path:  # pragma: no cover
        print("  (提示：Agent 使用了不同的案例库实例)")
    r = agent.run("客户反馈订单少发了一瓶玻璃水，要求补发。", customer="演示客户")
    cases = r.get("rag_cases") or []
    print(f"\n本次召回历史案例 {len(cases)} 条：")
    for h in cases:
        print(f"  - {h['ticket_id']}（相似度 {h['score']}）{h['description'][:40]}")
    print(f"\n建议摘要: {(r.get('suggestions') or [{}])[0].get('summary', '(无)')}")


if __name__ == "__main__":
    main()
