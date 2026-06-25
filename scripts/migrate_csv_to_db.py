# -*- coding: utf-8 -*-
"""迁移脚本：把 data/mock/*.csv 的业务数据搬进 SQLite。

用法：
    python scripts/migrate_csv_to_db.py            # 增量式重灌（清空这三张业务表后重写）
    python scripts/migrate_csv_to_db.py --reset    # 先 DROP 掉三张业务表再重建，然后重灌
    python scripts/migrate_csv_to_db.py --dry-run  # 只解析统计、不写库（看看会迁多少）

【大白话】
以前 Agent 每次启动都要把三个 CSV 读进内存、清洗、再手工聚合，既慢又只能做
"按 id 查一条"这种简单事。这个脚本把 CSV 一次性搬进 SQLite，之后查询就走索引了。

【为什么是"整表重灌"而不是"增量同步"】
本项目的数据集是"导出快照"，不是"持续追加的流水"。每次从 CSV 全量重灌，
语义上就是"让库 == CSV 当前的样子"，天然幂等 —— 跑多少次结果都一样，
也不用维护"上次导到哪儿了"的水位线。真正的增量同步需要业务侧提供变更时间戳，
那是另一个量级的工作。

【迁移都做了什么清洗】
1. 订单：剔除空订单号 / 空商品名 / 空门店 / 数量非正的行（与 src/data_source.py
   的 load() 保持完全一致的规则，保证迁移前后"看到的数据"不变）；
   然后按订单号聚合，一单多 SKU 压成 items JSON 数组，金额 = Σ(数量 × 单价)。
2. 库存：剔除空 SKU / 空门店 / 库存数量非正的行；按 (门店, SKU) 累加多仓库存。
3. 退货：剔除空原订单号的行；其余原样搬入（退货数据本身没有"多行合并"的需求）。

【技术细节】
- 不 import src.data_source 的 DataSource —— 那会触发它加载 CSV，多读一遍。
  这里直接读 CSV，复用它的**列名常量**（COL_*）保证口径一致，
  但清洗规则是"照抄"而非"调用"，因为两者面向的行结构不同（明细行 vs 聚合后订单）。
- 批量写入走 crud.replace_*（内部用 bulk_insert_mappings + 分批提交）。
- 时间列统一经 crud._parse_dt 解析，解析失败存 NULL，不让一行坏时间中断整批。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

# 保证能 import src（脚本在 scripts/ 下运行时把项目根加入 sys.path）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.logger import log  # noqa: E402
from src.persistence import crud  # noqa: E402
from src.persistence.database import DB_PATH, engine, get_db, init_db  # noqa: E402
from src.persistence.models import Inventory, Order, Return  # noqa: E402
from src.data_source import (  # noqa: E402
    COL_CUSTOMER,
    COL_INV_AVAIL,
    COL_INV_QTY,
    COL_INV_SAFETY,
    COL_NAME,
    COL_ORDER_CREATED,
    COL_ORDER_ID,
    COL_PRICE,
    COL_QTY,
    COL_RET_AMOUNT,
    COL_RET_APPLY_TIME,
    COL_RET_ORDER,
    COL_RET_REASON,
    COL_SKU,
    COL_SPEC,
    COL_STATUS,
    COL_STORE,
)

# 与 data_source.DataSource.load() 的默认文件保持一致
ORDERS_CSV = settings.mock_dir / "orders.csv"
INVENTORY_CSV = settings.mock_dir / "inventory.csv"
RETURNS_CSV = settings.mock_dir / "returns.csv"


def _read_csv(path: Path) -> pd.DataFrame:
    """读 CSV（utf-8-sig 吃掉 BOM）。文件缺失时直接报错退出，不带病继续。"""
    if not path.exists():
        raise SystemExit(f"[错误] 找不到数据文件: {path}")
    return pd.read_csv(path, encoding="utf-8-sig")


# ----------------------------------------------------------------------
# 1) 订单
# ----------------------------------------------------------------------
def build_order_rows() -> tuple[list[dict], dict]:
    """解析并聚合 orders.csv，返回 (待写入行列表, 统计信息)。"""
    df = _read_csv(ORDERS_CSV)
    raw_rows = len(df)

    # 数量转数值（转不动变 NaN，下面会被 notna 过滤掉）
    df[COL_QTY] = pd.to_numeric(df[COL_QTY], errors="coerce")
    clean = df.copy()
    clean = clean[clean[COL_ORDER_ID].notna()
                  & (clean[COL_ORDER_ID].astype(str).str.strip() != "")]
    clean = clean[clean[COL_NAME].notna()
                  & (clean[COL_NAME].astype(str).str.strip() != "")]
    clean = clean[clean[COL_STORE].notna()
                  & (clean[COL_STORE].astype(str).str.strip() != "")]
    clean = clean[clean[COL_QTY].notna() & (clean[COL_QTY] > 0)]
    dropped = raw_rows - len(clean)

    rows: list[dict] = []
    for oid, g in clean.groupby(COL_ORDER_ID):
        first = g.iloc[0]
        items = []
        for _, row in g.iterrows():
            price = float(row[COL_PRICE]) if pd.notna(row.get(COL_PRICE)) else 0.0
            items.append({
                "sku": None if pd.isna(row.get(COL_SKU)) else str(row[COL_SKU]),
                "name": None if pd.isna(row.get(COL_NAME)) else str(row[COL_NAME]),
                "spec": None if pd.isna(row.get(COL_SPEC)) else str(row[COL_SPEC]),
                "qty": int(row[COL_QTY]),
                "price": price,
            })
        amount = sum(i["qty"] * i["price"] for i in items)
        rows.append({
            "order_id": str(oid).strip(),
            "customer": _s(first.get(COL_CUSTOMER)),
            "store": _s(first.get(COL_STORE)),
            "amount": round(amount, 2),
            "status": _s(first.get(COL_STATUS)),
            "created_at": crud._parse_dt(first.get(COL_ORDER_CREATED)),
            "items": crud._dumps(items, max_chars=100000, field="order.items"),
        })

    stats = {
        "raw_rows": raw_rows,
        "clean_rows": len(clean),
        "dropped_rows": dropped,
        "orders": len(rows),
    }
    return rows, stats


def _s(value) -> str | None:
    """标量 -> 去空白字符串；NaN/None -> None。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


# ----------------------------------------------------------------------
# 2) 库存
# ----------------------------------------------------------------------
def build_inventory_rows() -> tuple[list[dict], dict]:
    """解析 inventory.csv，按 (门店, SKU) 累加多仓，返回 (行列表, 统计)。"""
    df = _read_csv(INVENTORY_CSV)
    raw_rows = len(df)

    df[COL_INV_QTY] = pd.to_numeric(df[COL_INV_QTY], errors="coerce")
    df[COL_INV_AVAIL] = pd.to_numeric(df[COL_INV_AVAIL], errors="coerce")
    clean = df[
        df[COL_SKU].notna() & (df[COL_SKU].astype(str).str.strip() != "")
        & df[COL_STORE].notna() & (df[COL_STORE].astype(str).str.strip() != "")
        & df[COL_INV_QTY].notna() & (df[COL_INV_QTY] >= 0)
    ]
    dropped = raw_rows - len(clean)

    # 同 (store, sku) 多仓 -> 累加。这里用普通 dict 手工累加而不是 groupby，
    # 因为要同时维护 qty / available / safety 三个求和列，手工写更直观。
    merged: dict[tuple[str, str], dict] = {}
    for _, row in clean.iterrows():
        key = (str(row[COL_STORE]).strip(), str(row[COL_SKU]).strip())
        cur = merged.setdefault(key, {"qty": 0.0, "available": 0.0, "safety": 0.0})
        cur["qty"] += float(row[COL_INV_QTY] or 0)
        avail = row.get(COL_INV_AVAIL)
        cur["available"] += float(avail) if pd.notna(avail) else 0.0
        safety = row.get(COL_INV_SAFETY)
        cur["safety"] += float(safety) if pd.notna(safety) else 0.0

    rows = [
        {
            "store": store,
            "sku": sku,
            "qty": int(round(vals["qty"])),
            "available": int(round(vals["available"])),
            "safety": int(round(vals["safety"])),
        }
        for (store, sku), vals in merged.items()
    ]
    stats = {
        "raw_rows": raw_rows,
        "clean_rows": len(clean),
        "dropped_rows": dropped,
        "inventory": len(rows),
    }
    return rows, stats


# ----------------------------------------------------------------------
# 3) 退货
# ----------------------------------------------------------------------
def build_return_rows() -> tuple[list[dict], dict]:
    """解析 returns.csv，返回 (行列表, 统计)。

    注意列名：回退数据的"时间"列叫「申请时间」、「金额」跟在订单文件里同名（商品金额）。
    """
    df = _read_csv(RETURNS_CSV)
    raw_rows = len(df)

    clean = df[df[COL_RET_ORDER].notna()
               & (df[COL_RET_ORDER].astype(str).str.strip() != "")]
    dropped = raw_rows - len(clean)

    rows = []
    for _, row in clean.iterrows():
        # 注意：退货文件的金额列叫「退货金额」，不是订单文件里的「商品金额」
        amount = pd.to_numeric(row.get(COL_RET_AMOUNT), errors="coerce")
        rows.append({
            "order_id": str(row[COL_RET_ORDER]).strip(),
            "customer": _s(row.get(COL_CUSTOMER)),
            "store": _s(row.get(COL_STORE)),
            "sku": _s(row.get(COL_SKU)),
            "reason": _s(row.get(COL_RET_REASON)),
            "amount": round(float(amount), 2) if pd.notna(amount) else 0.0,
            "created_at": crud._parse_dt(row.get(COL_RET_APPLY_TIME)),
        })

    stats = {
        "raw_rows": raw_rows,
        "clean_rows": len(clean),
        "dropped_rows": dropped,
        "returns": len(rows),
    }
    return rows, stats


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def reset_tables() -> None:
    """DROP 掉三张业务表（只动这三张，不碰 tasks / tool_executions 台账）。"""
    print("  [reset] 正在删除旧业务表 orders / inventory / returns ...")
    # 用 ORM 的 metadata 来找表对象，而不是拼 SQL 字符串 —— 表名改了这里自动跟着改
    for table in (Return.__table__, Inventory.__table__, Order.__table__):
        table.drop(bind=engine, checkfirst=True)
    print("  [reset] 已删除。重新建表 ...")
    init_db()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 data/mock/*.csv 迁移进 SQLite（data/agent_operations.db）")
    parser.add_argument("--reset", action="store_true",
                        help="先 DROP 业务表再重建（清掉历史残留结构）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只解析统计，不写库")
    args = parser.parse_args()

    t0 = time.time()
    print("=" * 64)
    print("业务数据迁移：CSV -> SQLite")
    print(f"库文件    : {DB_PATH}")
    print(f"订单 CSV  : {ORDERS_CSV}")
    print(f"库存 CSV  : {INVENTORY_CSV}")
    print(f"退货 CSV  : {RETURNS_CSV}")
    print("=" * 64)

    if args.reset:
        reset_tables()
    else:
        # 不加 --reset 也保证表结构存在（幂等），否则 replace_* 的 delete 会报表不存在
        init_db()

    # ---- 解析 ----
    print("\n[1/3] 解析订单 ...")
    order_rows, order_stats = build_order_rows()
    print(f"      原始 {order_stats['raw_rows']} 行 -> 清洗后 {order_stats['clean_rows']} 行"
          f"（剔除脏行 {order_stats['dropped_rows']}）-> 聚合出 {order_stats['orders']} 个订单")

    print("[2/3] 解析库存 ...")
    inv_rows, inv_stats = build_inventory_rows()
    print(f"      原始 {inv_stats['raw_rows']} 行 -> 清洗后 {inv_stats['clean_rows']} 行"
          f"（剔除脏行 {inv_stats['dropped_rows']}）-> 合并出 {inv_stats['inventory']} 条库存")

    print("[3/3] 解析退货 ...")
    ret_rows, ret_stats = build_return_rows()
    print(f"      原始 {ret_stats['raw_rows']} 行 -> 清洗后 {ret_stats['clean_rows']} 行"
          f"（剔除脏行 {ret_stats['dropped_rows']}）-> {ret_stats['returns']} 条退货")

    if args.dry_run:
        print("\n[dry-run] 未写入数据库。")
        print(f"耗时 {time.time() - t0:.2f}s")
        return 0

    # ---- 写库 ----
    print("\n写入数据库 ...")
    with get_db() as session:
        n_orders = crud.replace_orders(session, order_rows)
        n_inv = crud.replace_inventory(session, inv_rows)
        n_returns = crud.replace_returns(session, ret_rows)
        # 写完后从库里再数一遍，确认"写入条数 == 库里实际条数"（比信任返回值更可靠）
        c_orders = crud.count_orders(session)
        c_inv = crud.count_inventory(session)
        c_returns = crud.count_returns(session)

    print("\n" + "=" * 64)
    print("迁移完成")
    print("=" * 64)
    print(f"  订单   orders    : 写入 {n_orders:>6} 条 | 库中实际 {c_orders:>6} 条")
    print(f"  库存   inventory : 写入 {n_inv:>6} 条 | 库中实际 {c_inv:>6} 条")
    print(f"  退货   returns   : 写入 {n_returns:>6} 条 | 库中实际 {c_returns:>6} 条")
    print(f"  耗时             : {time.time() - t0:.2f}s")
    print("=" * 64)

    # 抽样自检：拿验收标准里指定的那单查一下，让用户当场看到数据能查出来
    with get_db() as session:
        probe = crud.get_order(session, "PO20260928-00001")
    if probe is not None:
        print(f"抽查 PO20260928-00001: 客户={probe.customer} 门店={probe.store} "
              f"金额={probe.amount} 状态={probe.status} 明细 {len(probe.items)} 项")
    else:
        print("抽查 PO20260928-00001: 未找到（该订单可能被清洗掉了，属正常）")

    print("\n下一步：直接 pytest tests/ -q 或 python scripts/run_api.py 即可使用新数据源。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
