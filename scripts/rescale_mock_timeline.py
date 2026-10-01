# -*- coding: utf-8 -*-
"""一次性工具：把演示数据的时间轴搬到"最近 N 天"，并把退货率降到合理区间。

【为什么要做这件事】
data/mock 里的订单/退货数据日期跨度是 2026-09 ~ 2027-12，相对"今天"
绝大多数都是**未来日期**（实测退货单 99.3% 在未来）。后果有两个：

1. "近 30 天退货率"这类窗口统计失去意义 —— 只写下界的时间窗会把未来数据
   当成"最近发生"全部算进来，于是"近 30 天"实际等于"全量"；
2. 退货率高达 87%（2436 个订单里 2117 个有退货记录），而 RETURN_RISK_
   THRESHOLD_CUSTOMER 默认是 0.4 —— 几乎所有客户都会被判成"升级人工"，
   Agent 的自动处理能力形同虚设。

【本脚本做什么】
- orders.csv：保留全部行（含脏数据行，清洗比例不变），只把
  「订单创建时间 / 付款时间」按原有**相对顺序**线性重映射到
  [今天-N天, 今天-1天]；
- returns.csv：按目标退货率从原订单号里抽样，只保留被抽中订单的退货记录，
  并把「申请时间 / 处理时间」重映射到"订单之后、今天之前"。

【安全性】
- 原文件先备份成 `<原名>.orig.bak`，脚本每次都基于**备份**重算，可重复运行；
- 不改动任何业务字段（订单号、商品、金额、数量、原因…），只动时间列；
- 不改动 orders 的行数，因此"脏数据清洗比例"这类既有断言不受影响。
"""
from __future__ import annotations

import argparse
import random
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
MOCK_DIR = ROOT / "data" / "mock"

ORDERS_FILE = MOCK_DIR / "orders.csv"
RETURNS_FILE = MOCK_DIR / "returns.csv"

_TS_FMT = "%Y-%m-%d %H:%M:%S"
_DATE_FMTS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S")


def _parse(series: pd.Series) -> pd.Series:
    """尽力解析时间列（与 crud._parse_dt 同口径），解析不出为 NaT。"""
    return pd.to_datetime(series.astype(str).str.strip(), errors="coerce", format="mixed")


def _backup(path: Path) -> Path:
    """把原文件备份一次（已存在备份则不覆盖 —— 备份始终是"最初的原始文件"）。"""
    bak = path.with_suffix(path.suffix + ".orig.bak")
    if not bak.exists():
        shutil.copy2(path, bak)
        print(f"  已备份 {path.name} -> {bak.name}")
    return bak


def rescale_orders(days: int) -> dict[str, datetime]:
    """重排订单时间轴，返回 {订单号: 新的下单时间}。"""
    src = _backup(ORDERS_FILE)
    df = pd.read_csv(src, encoding="utf-8-sig")

    created = _parse(df["订单创建时间"])
    valid = created.notna()
    n_valid = int(valid.sum())

    now = datetime.now()
    end = now - timedelta(days=1)
    start = now - timedelta(days=days)

    # 按"原来的先后顺序"线性重映射，保持相对次序（早的单子仍然更早）
    order_idx = created[valid].sort_values().index
    mapping: dict[int, datetime] = {}
    span = (end - start).total_seconds()
    for i, idx in enumerate(order_idx):
        frac = i / max(1, n_valid - 1)
        mapping[idx] = start + timedelta(seconds=span * frac)

    new_created = created.copy()
    for idx, ts in mapping.items():
        new_created.at[idx] = ts

    df["订单创建时间"] = new_created.dt.strftime(_TS_FMT).where(new_created.notna(), "")

    # 付款时间：下单后几分钟内
    rng = random.Random(20261001)
    paid = new_created.copy()
    for idx in mapping:
        paid.at[idx] = mapping[idx] + timedelta(minutes=rng.randint(1, 20))
    df["付款时间"] = paid.dt.strftime(_TS_FMT).where(paid.notna(), "")

    df.to_csv(ORDERS_FILE, index=False, encoding="utf-8-sig")

    order_dates: dict[str, datetime] = {}
    for idx, ts in mapping.items():
        oid = str(df.at[idx, "订单号"]).strip()
        if oid:
            order_dates.setdefault(oid, ts)
    print(f"  orders.csv : {len(df)} 行，{n_valid} 条订单时间已重排到最近 {days} 天内")
    return order_dates


def rescale_returns(order_dates: dict[str, datetime], target_rate: float) -> None:
    """按目标退货率抽样退货记录，并把申请时间搬到"订单之后、今天之前"。"""
    src = _backup(RETURNS_FILE)
    df = pd.read_csv(src, encoding="utf-8-sig")

    df["_oid"] = df["原订单号"].astype(str).str.strip()
    known = df[df["_oid"].isin(order_dates)]
    unique_orders = sorted(known["_oid"].unique())

    rng = random.Random(20261001)
    target = max(1, int(round(len(order_dates) * target_rate)))
    chosen = set(rng.sample(unique_orders, min(target, len(unique_orders))))

    kept_rows = []
    seen: set[str] = set()
    now = datetime.now()
    for _, row in known.iterrows():
        oid = row["_oid"]
        # 同一订单只保留一条退货记录：退货率按"订单"口径统计，
        # 一个订单留多条会人为抬高命中面
        if oid not in chosen or oid in seen:
            continue
        seen.add(oid)
        kept_rows.append(row)

    out = pd.DataFrame(kept_rows).drop(columns=["_oid"])

    apply_ts: list[str] = []
    handle_ts: list[str] = []
    for _, row in out.iterrows():
        oid = str(row["原订单号"]).strip()
        base = order_dates.get(oid, now - timedelta(days=30))
        # 申请时间：下单后 1~25 天，且不晚于"昨天"
        latest = now - timedelta(days=1)
        earliest = base + timedelta(days=1)
        if earliest > latest:
            earliest = latest
        delta = (latest - earliest).total_seconds()
        applied = earliest + timedelta(seconds=rng.uniform(0, max(0.0, delta)))
        handled = min(applied + timedelta(days=rng.randint(1, 3)), latest)
        apply_ts.append(applied.strftime(_TS_FMT))
        handle_ts.append(handled.strftime(_TS_FMT))

    out["申请时间"] = apply_ts
    out["处理时间"] = handle_ts
    out.to_csv(RETURNS_FILE, index=False, encoding="utf-8-sig")

    rate = len(chosen) / max(1, len(order_dates))
    print(f"  returns.csv: {len(df)} -> {len(out)} 行，"
          f"覆盖 {len(chosen)} 个订单（退货率 {rate:.1%}）")


def main() -> None:
    parser = argparse.ArgumentParser(description="重标定 data/mock 演示数据的时间轴与退货率")
    parser.add_argument("--days", type=int, default=180,
                        help="订单时间轴铺开的天数（默认 180）")
    parser.add_argument("--return-rate", type=float, default=0.12,
                        help="目标整体退货率（默认 0.12）")
    args = parser.parse_args()

    print(f"重标定演示数据：订单铺开 {args.days} 天，目标退货率 {args.return_rate:.0%}")
    order_dates = rescale_orders(args.days)
    rescale_returns(order_dates, args.return_rate)
    print("完成。下一步：python scripts/migrate_csv_to_db.py --reset")


if __name__ == "__main__":
    main()
