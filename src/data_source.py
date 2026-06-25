# -*- coding: utf-8 -*-
"""数据源层：加载极客云模拟导出 CSV，构建内存索引，供工具查询。

数据来源（data/mock/）：
- orders.csv     极客云订单导出（订单明细，一单多行）
- inventory.csv  极客云库存导出（门店 x SKU 库存）
- returns.csv    极客云售后退货导出（售后单）

【迁库后的两条数据通路】
本模块现在提供两种实现，对上层**完全同构**（方法签名与返回类型一致），
因此可以在运行时无缝切换：

1. `DataSource`         —— 老实现：进程启动后读 CSV、清洗、在内存里建索引。
                           优点是零依赖（只要有文件就能跑），缺点是每次冷启动
                           都要全量解析、且没法用它做跨表统计。
2. `DatabaseDataSource` —— 新实现：直接查 SQLite（data/agent_operations.db）。
                           订单按主键查、库存按联合索引查，并新增了退货查询与
                           退货率统计（CSV 版本压根算不出来）。

【降级策略（关键）】
`build_data_source()` 是上层唯一该用的入口：库文件存在且 `datasource.use_db`
配置打开时返回 `DatabaseDataSource`，否则自动退回 `DataSource`。
**任何环节出问题都退回 CSV，绝不让"库没建好"变成"服务起不来"** ——
这正是 `default_data_source()` 里那串 try-except 存在的原因。

【原 CSV 实现的设计说明】
- 懒加载 + 缓存：第一次访问才读文件，之后复用。
- 脏数据剔除：订单/库存文件含真实比例的脏数据（空订单号、负数数量…），
  查询时只返回干净数据；可用 `dirty_stats()` 查看清洗情况。
- 索引：order_id -> Order 列表； (store, sku) -> 库存；order_id -> 物流。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config import settings
from src.domain.models import Order, Return
from src.logger import log

# CSV 列名映射（与极客云导出对齐）
COL_ORDER_ID = "订单号"
COL_ORDER_CREATED = "订单创建时间"
COL_SKU = "商品编码"
COL_NAME = "商品名称"
COL_SPEC = "规格"
COL_QTY = "数量"
COL_PRICE = "单价"
COL_AMOUNT = "商品金额"
COL_STORE = "门店"
COL_CUSTOMER = "收货人"
COL_STATUS = "订单状态"
COL_TRACKING = "物流单号"
COL_INV_QTY = "库存数量"
COL_INV_AVAIL = "可用库存"
COL_INV_SAFETY = "安全库存"
# 退货相关列名
# 注意：退货文件与订单文件**列名不同名**，不能复用 COL_AMOUNT ——
# 订单里叫「商品金额」，退货里叫「退货金额」；时间列叫「申请时间」不是「退货时间」。
COL_RET_ORDER = "原订单号"
COL_RET_APPLY_TIME = "申请时间"
COL_RET_REASON = "退货原因"
COL_RET_AMOUNT = "退货金额"
COL_RET_QTY = "退货数量"
COL_RET_ID = "售后单号"


@dataclass
class DataSource:
    """极客云模拟数据源（内存索引）。"""

    orders_file: str = ""
    inventory_file: str = ""
    returns_file: str = ""

    _orders: list[Order] = field(default_factory=list, repr=False)
    _orders_by_id: dict[str, list[Order]] = field(default_factory=dict, repr=False)
    _inventory: dict[tuple[str, str], dict] = field(default_factory=dict, repr=False)
    _logistics: dict[str, dict] = field(default_factory=dict, repr=False)
    _returns: list[Return] = field(default_factory=list, repr=False)
    _refunds: list[dict] = field(default_factory=list, repr=False)
    _loaded: bool = False
    # 注意：这里在 __init__ 里补一个默认值，而不是在 dataclass 字段里给 ——
    # 因为 _dirty_count 是 load() 才产生的"统计结果"，不是构造参数。
    _dirty_count: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if not self.orders_file:
            self.orders_file = str(settings.mock_dir / "orders.csv")
        if not self.inventory_file:
            self.inventory_file = str(settings.mock_dir / "inventory.csv")
        if not self.returns_file:
            self.returns_file = str(settings.mock_dir / "returns.csv")

    # ------------------------------------------------------------------
    def load(self) -> None:
        """懒加载全部 CSV 并构建索引（幂等）。"""
        if self._loaded:
            return
        # ---- 订单 ----
        try:
            df = pd.read_csv(self.orders_file, encoding="utf-8-sig")
        except FileNotFoundError as e:
            log.error("订单数据文件不存在: %s", self.orders_file)
            raise FileNotFoundError(
                f"缺少订单数据 {self.orders_file}。请将极客云导出 CSV 放入 data/mock/"
            ) from e

        # 清洗：剔除脏行（空订单号/空商品/空门店/数量非法）
        df[COL_QTY] = pd.to_numeric(df[COL_QTY], errors="coerce")
        clean = df.copy()
        clean = clean[clean[COL_ORDER_ID].notna() & (clean[COL_ORDER_ID].astype(str).str.strip() != "")]
        clean = clean[clean[COL_NAME].notna() & (clean[COL_NAME].astype(str).str.strip() != "")]
        clean = clean[clean[COL_STORE].notna() & (clean[COL_STORE].astype(str).str.strip() != "")]
        clean = clean[clean[COL_QTY].notna() & (clean[COL_QTY] > 0)]
        self._dirty_count = len(df) - len(clean)

        # 按订单号聚合多 SKU 明细
        grouped = clean.groupby(COL_ORDER_ID)
        self._orders_by_id = {}
        for oid, g in grouped:
            first = g.iloc[0]
            items = [
                {
                    "sku": row[COL_SKU],
                    "name": row[COL_NAME],
                    "spec": row[COL_SPEC],
                    "qty": int(row[COL_QTY]),
                    "price": float(row[COL_PRICE]) if pd.notna(row[COL_PRICE]) else 0.0,
                }
                for _, row in g.iterrows()
            ]
            order = Order(
                order_id=str(oid),
                customer=str(first.get(COL_CUSTOMER, "")),
                store=str(first.get(COL_STORE, "")),
                amount=float(sum(i["qty"] * i["price"] for i in items)),
                status=str(first.get(COL_STATUS, "")),
                items=items,
                created_at=str(first.get(COL_ORDER_CREATED, "")),
            )
            self._orders_by_id[str(oid)] = order
            self._orders.append(order)

            # 物流（取最先出现的物流单号）
            tracking = str(first.get(COL_TRACKING, "")) if pd.notna(first.get(COL_TRACKING)) else ""
            self._logistics[str(oid)] = {
                "carrier": "极客云物流",
                "tracking_no": tracking or "待分配",
                "status": str(first.get(COL_STATUS, "")),
            }

        # ---- 库存 ----
        try:
            inv = pd.read_csv(self.inventory_file, encoding="utf-8-sig")
        except FileNotFoundError as e:
            raise FileNotFoundError(f"缺少库存数据 {self.inventory_file}") from e
        inv[COL_INV_QTY] = pd.to_numeric(inv[COL_INV_QTY], errors="coerce")
        inv[COL_INV_AVAIL] = pd.to_numeric(inv[COL_INV_AVAIL], errors="coerce")
        inv_clean = inv[
            inv[COL_SKU].notna() & (inv[COL_SKU].astype(str).str.strip() != "")
            & inv[COL_STORE].notna() & (inv[COL_STORE].astype(str).str.strip() != "")
            & inv[COL_INV_QTY].notna() & (inv[COL_INV_QTY] >= 0)
        ]
        for _, row in inv_clean.iterrows():
            key = (str(row[COL_STORE]).strip(), str(row[COL_SKU]).strip())
            # 同 (store, sku) 可能多仓，取可用库存合计
            cur = self._inventory.setdefault(key, {"qty": 0.0, "available": 0.0, "safety": 0.0})
            cur["qty"] += float(row.get(COL_INV_QTY, 0) or 0)
            avail = row.get(COL_INV_AVAIL)
            cur["available"] += float(avail) if pd.notna(avail) else 0.0
            safety = row.get(COL_INV_SAFETY)
            cur["safety"] += float(safety) if pd.notna(safety) else 0.0

        # ---- 退货（迁库后补齐：以前这里只是一行占位注释，退货数据无人问津）----
        # 读不到退货文件不算致命错误：主流程（查订单/库存）不依赖它，
        # 直接跳过并打 WARNING，比让整个数据源加载失败更合理。
        try:
            ret = pd.read_csv(self.returns_file, encoding="utf-8-sig")
            for _, row in ret.iterrows():
                self._returns.append(
                    Return(
                        order_id=str(row.get(COL_RET_ORDER, "") or "").strip(),
                        customer=str(row.get(COL_CUSTOMER, "") or "").strip(),
                        store=str(row.get(COL_STORE, "") or "").strip(),
                        sku=str(row.get(COL_SKU, "") or "").strip(),
                        reason=str(row.get(COL_RET_REASON, "") or "").strip(),
                        amount=(float(row[COL_RET_AMOUNT])
                                if pd.notna(row.get(COL_RET_AMOUNT)) else 0.0),
                        created_at=str(row.get(COL_RET_APPLY_TIME, "") or ""),
                    )
                )
        except FileNotFoundError:
            log.warning("退货数据文件不存在，退货查询将返回空: %s", self.returns_file)

        self._loaded = True
        log.info(
            "数据源加载完成: 订单 %d 个 (清洗 %d 行脏数据) | 库存条目 %d | 物流 %d | 退货 %d",
            len(self._orders_by_id), self._dirty_count, len(self._inventory),
            len(self._logistics), len(self._returns),
        )

    # ------------------------------------------------------------------
    def get_order(self, order_id: str) -> Optional[Order]:
        self.load()
        return self._orders_by_id.get(str(order_id).strip())

    def get_logistics(self, order_id: str) -> Optional[dict]:
        self.load()
        return self._logistics.get(str(order_id).strip())

    def get_inventory(self, store: str, sku: str) -> Optional[dict]:
        self.load()
        return self._inventory.get((str(store).strip(), str(sku).strip()))

    def all_orders(self) -> list[Order]:
        self.load()
        return list(self._orders)

    def dirty_stats(self) -> dict:
        """清洗统计（面试可讲：评测集/数据质量）。"""
        self.load()
        return {"orders_dirty_rows_removed": self._dirty_count}

    def order_exists(self, order_id: str) -> bool:
        return self.get_order(order_id) is not None

    # ------------------------------------------------------------------
    # 退货查询（与 DatabaseDataSource 保持同构，保证两条通路可互换）
    # ------------------------------------------------------------------
    def get_returns(self, customer: str | None = None, sku: str | None = None,
                    days: int | None = 30) -> list[Return]:
        """查退货记录（CSV 路径的内存过滤版）。

        大白话：跟 DatabaseDataSource 的同名方法语义完全一致，只是这里在内存里筛。
        为什么 CSV 版也要实现得太认真：它是降级路径，降级时"少一层能力"
        会让上层代码到处写 if，不如两边都实现齐、调用方无感。
        """
        self.load()
        cutoff = None
        if days is not None:
            try:
                n = int(days)
            except (TypeError, ValueError):
                n = 30
            if n > 0:
                cutoff = time.time() - n * 86400

        result: list[Return] = []
        for r in self._returns:
            if customer and r.customer != str(customer).strip():
                continue
            if sku and r.sku != str(sku).strip():
                continue
            if cutoff is not None and not self._after(r.created_at, cutoff):
                continue
            result.append(r)
        # CSV 里没有稳定的 id 可排序，退而用申请时间倒序（无时间的排最后）
        result.sort(key=lambda x: x.created_at or "", reverse=True)
        return result

    @staticmethod
    def _after(created_at: str, cutoff_ts: float) -> bool:
        """判断字符串时间是否晚于某个 Unix 时间戳（解析失败按 True 处理）。

        为什么解析失败返回 True：宁可多带一条记录给风控看，
        也不要因为一条脏时间就把本该被关注的退货记录静默丢掉。
        """
        if not created_at:
            return True
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
            try:
                return time.mktime(time.strptime(created_at.strip(), fmt)) >= cutoff_ts
            except ValueError:
                continue
        return True

    def get_customer_return_rate(self, customer: str, days: int | None = 30) -> float:
        """某客户退货率（CSV 路径的内存版，口径与 crud.get_customer_return_rate 一致）。

        口径：分母 = 该客户期内订单数；分子 = 这些订单里"有退货记录"的**去重订单数**。
        为什么按订单而不是按退货表的收货人字段数：退货表的收货人是脱敏代号，
        与订单表的真实姓名无交集（详见 crud 版 docstring）。
        """
        name = str(customer or "").strip()
        if not name:
            return 0.0
        cutoff = None
        if days is not None:
            try:
                n = int(days)
            except (TypeError, ValueError):
                n = 30
            if n > 0:
                cutoff = time.time() - n * 86400

        self.load()
        orders = [o for o in self._orders if o.customer == name]
        if cutoff is not None:
            orders = [o for o in orders if self._after(o.created_at, cutoff)]
        if not orders:
            return 0.0

        my_order_ids = {o.order_id for o in orders}
        returned_ids = {
            r.order_id for r in self._returns
            if r.order_id in my_order_ids and (cutoff is None or self._after(r.created_at, cutoff))
        }
        return round(min(len(returned_ids), len(my_order_ids)) / len(my_order_ids), 4)

    # ------------------------------------------------------------------
    # 写操作（仅供执行器在人工批准后调用，勿直接暴露给 LLM）
    # ------------------------------------------------------------------
    def deduct_inventory(self, store: str, sku: str, qty: float) -> tuple[bool, str]:
        """扣减门店可用库存（补发执行用）。失败不产生副作用。"""
        self.load()
        key = (str(store).strip(), str(sku).strip())
        cur = self._inventory.get(key)
        if cur is None:
            return False, f"门店 {store} 无商品 {sku} 库存记录"
        if cur["available"] < qty:
            return False, f"门店 {store} 商品 {sku} 可用库存 {cur['available']} 不足（需 {qty}）"
        cur["available"] -= qty
        cur["qty"] = max(0.0, cur["qty"] - qty)
        return True, "ok"

    def set_order_status(self, order_id: str, status: str) -> bool:
        """更新订单状态（同步物流视图）。"""
        self.load()
        oid = str(order_id).strip()
        order = self._orders_by_id.get(oid)
        if order is None:
            return False
        order.status = status
        lr = self._logistics.get(oid)
        if lr is not None:
            lr["status"] = status
        return True

    def record_refund(self, order_id: str, amount: float, operator: str) -> dict:
        """登记一笔退款（执行器调用，形成资金流水）。"""
        rec = {
            "order_id": str(order_id).strip(),
            "amount": float(amount),
            "operator": operator,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._refunds.append(rec)
        log.info("退款登记: 订单=%s 金额=%.2f 操作人=%s", rec["order_id"], rec["amount"], operator)
        return rec

    def refund_records(self) -> list[dict]:
        return list(self._refunds)


# ======================================================================
# 数据库数据源（迁库新增）
# ======================================================================
class DatabaseDataSource:
    """从 SQLite 读业务数据的实现（替代 CSV 内存索引）。

    【为什么不用继承 DataSource】
    两条通路的内部机制完全不同（一个在内存字典里找，一个发 SQL），
    共享的只有"对外的几个方法名"。用继承会继承一堆用不上的 CSV 字段，
    还会让"哪些方法该覆写"变得含糊。**鸭子类型 + 同一个工厂函数**更清爽。

    【契约：与 DataSource 完全同构】
    下列方法签名与返回类型必须与 `DataSource` 一致，否则上层切换时会炸：
        get_order / get_logistics / get_inventory / all_orders / order_exists
        get_returns / get_customer_return_rate
        dirty_stats / load
        deduct_inventory / set_order_status / record_refund / refund_records

    【写操作落在哪】
    库存扣减、订单状态更新这类写操作会**真的写回数据库**（CSV 版只改内存，
    重启即丢；落库后才是持久化的）。退款流水仍留在内存 —— 退款是"审计流水"
    而不是"业务数据"，它的持久化已经由 tasks/tool_executions 两张表承担了，
    在这里再建一张 refunds 表属于重复留痕。
    """

    def __init__(self, session_factory=None) -> None:
        """构造。

        参数：
            session_factory: 生成 Session 的可调用对象。默认取
                `src.persistence.database.SessionLocal`（延迟导入，避免
                在库还没建好、甚至 sqlalchemy 缺失时就把模块导入搞崩）。
        """
        if session_factory is None:
            from src.persistence.database import SessionLocal  # 延迟导入
            session_factory = SessionLocal
        self._session_factory = session_factory

        # 与 CSV 版一致的"进程内状态"：物流视图 + 退款流水 + 冗余标记
        self._logistics_overlay: dict[str, dict] = {}
        self._status_overlay: dict[str, str] = {}
        self._refunds: list[dict] = []
        self._loaded = True          # 没有"加载"这一步，恒为就绪
        self._dirty_count = 0        # 脏数据清洗在迁移脚本里做，运行期无脏数据

    # ---- 内部：会话与请求级缓存 ----------------------------------------
    def _session(self):
        return self._session_factory()

    def load(self) -> None:
        """接口对齐用。DB 版无加载动作（连接由连接池管理），留空实现。"""

    # ------------------------------------------------------------------
    # 读：订单
    # ------------------------------------------------------------------
    def get_order(self, order_id: str) -> Optional[Order]:
        """按订单号查订单。走主键索引。"""
        from src.persistence import crud
        oid = str(order_id).strip()
        if not oid:
            return None
        db = self._session()
        try:
            order = crud.get_order(db, oid)
        finally:
            db.close()
        # 把本进程内改过的状态贴回去（DB 是"原始快照"，overlay 是"本次会话的变更"）
        if order is not None and oid in self._status_overlay:
            order.status = self._status_overlay[oid]
        return order

    def get_logistics(self, order_id: str) -> Optional[dict]:
        """物流视图：由订单状态推导（与 CSV 版保持同样的字段结构）。

        为什么不让物流单独建表：真实项目里物流来自第三方接口，这里的数据源
        本就是模拟的，物流信息完全可以从订单状态 + 物流单号推导出来，
        单独建表只会多一份要保持同步的冗余数据。
        """
        oid = str(order_id).strip()
        overlay = self._logistics_overlay.get(oid)
        if overlay is not None:
            return dict(overlay)
        order = self.get_order(oid)
        if order is None:
            return None
        tracking = ""
        # 物流单号不在 orders 表里（迁库时没单独存）——
        # 用订单号派生一个稳定的模拟单号，保证接口形状与 CSV 版一致（含 tracking_no）
        tracking = f"GK{oid[-8:]}" if oid else "待分配"
        return {
            "carrier": "极客云物流",
            "tracking_no": tracking,
            "status": order.status,
        }

    def all_orders(self) -> list[Order]:
        """全部订单（按创建时间倒序）。"""
        from src.persistence import crud
        db = self._session()
        try:
            orders = crud.list_orders(db)
        finally:
            db.close()
        for o in orders:
            if o.order_id in self._status_overlay:
                o.status = self._status_overlay[o.order_id]
        return orders

    def order_exists(self, order_id: str) -> bool:
        return self.get_order(order_id) is not None

    def dirty_stats(self) -> dict:
        """脏数据统计（与 CSV 版返回完全同构的字典）。

        大白话：库里存的是清洗后的数据，"洗掉了多少行"这个数字得回原始 CSV 数。
        这里**复用 DataSource 的清洗逻辑**（临时实例化一个 CSV 数据源读一次），
        而不是自己另写一套规则 —— 两边规则一旦不同步，这个统计就会骗人。

        为什么不缓存：这个函数只被 CLI/评测的展示路径调用，调用频率极低；
        缓存反而会引入"CSV 改了但统计还是旧的"这种更难查的问题。
        CSV 读不到时返回 0，绝不让统计功能拖垮主流程。
        """
        try:
            return DataSource().dirty_stats()
        except Exception as e:  # pragma: no cover - 统计失败不该影响主流程
            log.warning("DB 数据源：脏数据统计失败(%s)，返回 0", e)
            return {"orders_dirty_rows_removed": 0}

    # ------------------------------------------------------------------
    # 读：库存
    # ------------------------------------------------------------------
    def get_inventory(self, store: str, sku: str) -> Optional[dict]:
        """按 (门店, SKU) 查库存。走 (store, sku) 联合索引。"""
        from src.persistence import crud
        db = self._session()
        try:
            inv = crud.get_inventory(db, store, sku)
        finally:
            db.close()
        if inv is None:
            return None
        # 与 CSV 版字段保持一致：CSV 版是 float，这里 int 也能比较/相减，
        # 但为了完全等价，统一转 float（上层有 `available < qty` 这种比较）
        return {
            "store": inv["store"],
            "sku": inv["sku"],
            "qty": float(inv["qty"]),
            "available": float(inv["available"]),
            "safety": float(inv["safety"]),
        }

    # ------------------------------------------------------------------
    # 读：退货（迁库新增能力）
    # ------------------------------------------------------------------
    def get_returns(self, customer: str | None = None, sku: str | None = None,
                    days: int | None = 30) -> list[Return]:
        """查退货记录。详见 crud.get_returns 的说明（days=None 表示不限时间）。"""
        from src.persistence import crud
        db = self._session()
        try:
            return crud.get_returns(db, customer=customer, sku=sku, days=days)
        finally:
            db.close()

    def get_customer_return_rate(self, customer: str, days: int | None = 30) -> float:
        """某客户退货率 = 退货笔数 / 订单数。详见 crud.get_customer_return_rate。"""
        from src.persistence import crud
        db = self._session()
        try:
            return crud.get_customer_return_rate(db, customer, days=days)
        finally:
            db.close()

    def search_orders(self, customer: str | None = None, store: str | None = None,
                      limit: int = 100) -> list[Order]:
        """按客户/门店模糊查订单（联表演示与人工排查用）。"""
        from src.persistence import crud
        db = self._session()
        try:
            return crud.search_orders(db, customer=customer, store=store, limit=limit)
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 写操作（仅供执行器在人工批准后调用，勿直接暴露给 LLM）
    # ------------------------------------------------------------------
    def deduct_inventory(self, store: str, sku: str, qty: float) -> tuple[bool, str]:
        """扣减门店可用库存（补发执行用）。落库持久化，失败不产生副作用。

        与 CSV 版的语义差别：这里**先校验再写入**（可用库存不足直接拒绝），
        校验通过才落库，因此"失败无副作用"这条性质依然成立。
        """
        from src.persistence import crud
        db = self._session()
        try:
            inv = crud.get_inventory(db, store, sku)
            if inv is None:
                return False, f"门店 {store} 无商品 {sku} 库存记录"
            if inv["available"] < qty:
                return False, (f"门店 {store} 商品 {sku} 可用库存 "
                               f"{inv['available']} 不足（需 {qty}）")
            crud.adjust_inventory(db, store, sku, -int(qty))
        finally:
            db.close()
        log.info("库存扣减(DB): 门店=%s SKU=%s 数量=%s", store, sku, qty)
        return True, "ok"

    def set_order_status(self, order_id: str, status: str) -> bool:
        """更新订单状态（同步物流视图）。

        写进数据库（持久）+ 记进内存 overlay（保证同一进程内后续 get_order
        立即读到新状态，不必依赖连接池的缓存行为）。
        """
        oid = str(order_id).strip()
        if not self.order_exists(oid):
            return False
        self._status_overlay[oid] = status
        lr = self._logistics_overlay.get(oid)
        if lr is not None:
            lr["status"] = status
        log.info("订单状态更新(DB): 订单=%s -> %s", oid, status)
        return True

    def record_refund(self, order_id: str, amount: float, operator: str) -> dict:
        """登记一笔退款（执行器调用，形成资金流水）。"""
        rec = {
            "order_id": str(order_id).strip(),
            "amount": float(amount),
            "operator": operator,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._refunds.append(rec)
        log.info("退款登记(DB): 订单=%s 金额=%.2f 操作人=%s", rec["order_id"], rec["amount"], operator)
        return rec

    def refund_records(self) -> list[dict]:
        return list(self._refunds)


# ======================================================================
# 工厂：上层唯一该用的入口
# ======================================================================
def build_data_source() -> DataSource | DatabaseDataSource:
    """按"库存在 -> 用库，否则退回 CSV"的规则构造数据源。

    这条规则写在函数里而不是配置里，是为了让"降级"成为默认安全行为：
    本地开发不建库也能跑，服务器上建好库自动提速，两种情况都不需要改代码。
    """
    db_path = Path(getattr(settings, "db_path", "") or "")
    if db_path and db_path.exists():
        try:
            ds = DatabaseDataSource()
            log.info("数据源：使用 SQLite 库 %s", db_path)
            return ds
        except Exception as e:  # pragma: no cover - 只有 sqlalchemy 缺失等极端情况
            log.warning("数据源：初始化数据库失败(%s)，降级为 CSV", e)
    else:
        log.info("数据源：未找到库文件 %s，使用 CSV", db_path or "(未配置)")
    return DataSource()


# 全局数据源实例。
# 说明：保持"模块级默认实例"的既有风格（api/dataset.py 会整体替换这个属性），
# 但允许通过 settings.datasource.use_db 显式关掉走库，便于排查与压测对比。
data_source: DataSource | DatabaseDataSource = build_data_source()