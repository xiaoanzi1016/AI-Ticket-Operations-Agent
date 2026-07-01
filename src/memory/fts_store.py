# -*- coding: utf-8 -*-
"""记忆层 · 全文检索（RAG）：历史工单案例的 FTS5 检索。

【这个文件是干什么的】
Agent 原来只会"按规则/按当前订单"给建议，看不到"上次类似的问题是怎么处理的"。
这个模块把每一条处理完的工单沉淀进 SQLite 的 FTS5 全文索引，新工单来时按关键词
召回 top-k 历史案例，注入 system prompt —— 建议从"规则版"升级为"案例驱动"。

  旧工单（问题+建议+结果） --写入--> FTS5 全文索引
                                        |
  新工单（问题） --MATCH--> bm25 排序 ---+--> top-k 案例 --> 注入 prompt

【为什么是 FTS5 而不是向量库】
- 零新依赖：FTS5 是 SQLite 内建模块，不需要 chromadb / torch / 模型文件。
- 零下载：没有"首次运行下载 470MB 模型"这种部署摩擦，离线环境开箱可用。
- 与业务库同文件：案例表就和 orders/returns 待在一起，备份/迁移都是一个文件。
代价是"只能关键词命中、不能语义泛化"（问"少了东西"匹配不到"少发"）——
对本项目"工单文本用词高度重复"的场景来说，这个代价可以接受。

【★ 中文分词：FTS5 最大的坑】
FTS5 内置的 `unicode61` 分词器**不切中文**：它把整句
"客户反馈少发一瓶玻璃水"当成**一个 token**，导致 `MATCH '玻璃水'` 永远匹配不到。
（这不是配置问题，是 unicode61 的设计 —— 它按 Unicode 的"字母/数字"边界切词，
中文没有空格，于是整句连成一片。）

解法（本模块采用，不引入任何依赖）：**写入和查询两侧都做 bigram 切词**。
  "玻璃水"      -> "玻璃 璃水"
  "客户反馈少发" -> "客户 户反 反馈 馈少 少发"
bigram（相邻两字）在中文检索里是经典权衡：比 unigram 更准（少召回噪声），
比整句更宽（能命中子串）。中英混排时英文/数字按原样保留。
两侧用**同一个函数**切词是正确性的前提 —— 见 `_segment()`。

【技术细节】
- 表结构按要求用 `tokenize='unicode61'`，切词在应用层做（写入前切、查询前切）。
- 检索用 `bm25()` 排序并按需换算成 [0,1] 的"相似度"分值供展示。
- 所有 SQL 用参数绑定（`?`），不拼字符串 —— 查询词来自用户输入，拼接会有注入面。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Optional

from src.logger import log

# FTS5 虚拟表名（与业务表 orders/inventory/returns 共存于同一个库文件）
TABLE_NAME = "ticket_cases"

# 中文区间（CJK 统一表意文字基本区）。用于把文本切成"中文段 / 非中文段"
_CJK = r"\u4e00-\u9fff"
_TOKEN_RE = re.compile(rf"[A-Za-z0-9]+|[{_CJK}]+")

# ----------------------------------------------------------------------
# 领域同义词表：把口语化说法映射到工单里的标准用词。
#
# 【为什么需要它】FTS5 是**字面匹配**，"东西碎了"匹配不到"破损" ——
# 纯关键词方案的召回会因此很脆。这里维护一张小映射表，
# 在**查询侧**把口语词展开成标准词一起搜，把召回率拉回可用水平。
# 为什么只做查询侧：案例入库时用的是规则版/LLM 生成的规范表述，
# 真正"用词不规范"的是用户随口说的话；查询侧扩展成本最低、收益最大。
# 为什么不做全量同义词库：过度扩展会把不相关案例也拉进来（准确率下降）。
# 这张表只收**本项目工单高频**的几组说法，够用即可。
# ----------------------------------------------------------------------
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "破损": ("碎了", "坏了", "破损", "损坏", "烂了", "压坏", "磕碰"),
    "少发": ("少发", "漏发", "少件", "缺件", "没发", "漏寄", "缺货"),
    "退款": ("退款", "退钱", "退货", "要退", "不想要"),
    "补发": ("补发", "重发", "再发", "补寄"),
    "物流": ("物流", "快递", "发货", "没收到", "未收到", "运单"),
}


def _expand_query(text: str) -> list[str]:
    """按同义词表扩展查询词，返回"原词 + 命中的标准词"列表（去重）。

    大白话：用户说"东西碎了"，我们额外拿"破损"再去搜一遍，提高召回。

    技术细节：只要原文里出现了某组同义词中的**任意一个**，
    就把该组的所有说法都加入扩展结果（组内互扩）。这样"碎了"能搜到
    "破损"，"破损"也能搜到只写了"坏了"的案例 —— 组内是等价语义。
    """
    src = str(text or "")
    extra: list[str] = []
    for _, group in _SYNONYMS.items():
        if any(w in src for w in group):
            extra.extend(group)
    # 去重但保持顺序（原词优先，保证精确命中的案例排前面）
    seen: set[str] = set()
    out: list[str] = []
    for w in extra:
        # ★ 过滤掉单字词：单字（如"退"）在 bigram 下会变成"退X"式碎片，
        # 极容易误命中不相关案例（实测"退"会把"破损退款"案例顶到最高分）。
        # 宁可少召回几条，也不要让噪声污染 top-k —— 注入给模型的是"参考案例"，
        # 错案例比没案例危害更大（模型可能照着错的做）。
        if len(w) < 2 or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _segment(text: str) -> str:
    """把文本切成 FTS5 能用的空格分隔 token 串（中文 bigram + 英文原样）。

    大白话：中文没有空格，FTS5 切不开；这里先手工切好，再用空格把 token 连起来
            喂给 FTS5。写入和查询必须走**同一个**函数，否则两边词汇对不上。

    技术细节：
    - 用正则把文本拆成"英文数字串"和"中文串"两类片段；
    - 中文串按相邻两字切（bigram），单字串原样保留；
    - 英文数字串转小写（unicode61 本身不区分大小写，但显式转换更保险）；
    - 这样 "少发一瓶玻璃水" -> "少发 发一 一瓶 瓶玻 玻璃 璃水"，
      查询 "玻璃水" -> "玻璃 璃水"，两者有交集即可命中。

    边界情况：空串返回空串；纯符号（如"！！！"）会切出空串 ——
    调用方需把空结果当作"不可检索"，否则 `MATCH ''` 会抛 FTS5 语法错。
    """
    if not text:
        return ""
    tokens: list[str] = []
    for part in _TOKEN_RE.findall(str(text)):
        if re.fullmatch(rf"[{_CJK}]+", part):
            if len(part) == 1:
                tokens.append(part)
            else:
                tokens.extend(part[i:i + 2] for i in range(len(part) - 1))
        else:
            tokens.append(part.lower())
    return " ".join(tokens)


def _display_width(text: str) -> int:
    """算字符串显示宽度（中文算 2 列），用于 CLI/日志里的对齐展示。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


class FtsCaseStore:
    """历史工单案例的 FTS5 全文检索存储。

    用法：
        store = FtsCaseStore()
        store.add_case("T-1", "客户反馈少发一瓶玻璃水", "补发同款", "成功执行", {...})
        hits = store.search("少发一瓶玻璃水", top_k=3)
        store.count()

    设计约定（与项目其它增强组件一致）：
    - **降级优先**：构造时若 FTS5 不可用（极老的 SQLite），只打 WARNING 并置
      `available=False`，所有方法静默返回空 —— 绝不 raise，不让增强项拖垮主流程。
    - **接口稳定**：方法名与签名按需求约定，调用方无需感知底层是 FTS5 还是别的。
    """

    def __init__(self, db_path: str = "data/agent_operations.db") -> None:
        """初始化：连库 + 建 FTS5 表（幂等）。

        参数：
            db_path: SQLite 库文件路径。默认指向业务库
                     `data/agent_operations.db`（与环境变量 AGENT_DB_PATH 一致）。

        技术细节：用标准库 sqlite3 直连，**不走 SQLAlchemy** ——
                 FTS5 是虚拟表，SQLAlchemy 的 ORM 映射对它没有意义，
                 而原生 SQL + 参数绑定已经足够安全、也更少一层依赖。
        """
        self.db_path = self._resolve_path(db_path)
        self.available: bool = False
        self.unavailable_reason: str = ""
        try:
            self._ensure_schema()
            self.available = True
            log.info("FTS：案例检索就绪 (%s, 现有案例 %d 条)", self.db_path, self.count())
        except Exception as e:
            self.unavailable_reason = f"{type(e).__name__}: {e}"
            log.warning("FTS：全文检索不可用，已降级为规则版 (%s)", self.unavailable_reason)

    # ------------------------------------------------------------------
    # 路径与 schema
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_path(db_path: str) -> str:
        """把库路径解析成绝对路径（相对路径按**项目根**解析）。

        为什么不按进程 cwd：从别的目录启动时会连到另一个库上，
        和 database.py / config.py 的处理保持一致，避免"两套库"的困惑。
        """
        p = Path(db_path)
        if not p.is_absolute():
            p = (_project_root() / db_path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)

    def _connect(self) -> sqlite3.Connection:
        """开一个连接（用完即关，不做连接池 —— 案例检索是低频操作）。"""
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def _ensure_schema(self) -> None:
        """建 FTS5 虚拟表（IF NOT EXISTS，幂等）。

        表结构按需求约定（ticket_id / description / suggestion / outcome / metadata，
        tokenize='unicode61'）。**不修改任何现有业务表** ——
        虚拟表是独立对象，与 orders/inventory/returns/tasks 互不影响。

        注意：这里额外加了一列 `seg`（切词后的文本）用于 MATCH，
        而原始列存可读原文。为什么不全用一列：
        如果直接把切词结果存进 description，检索结果展示出来就是
        "少发 发一 一瓶 ..." 这种碎片，没法读。分开存 = 检索准 + 展示好看。
        这是对需求表结构的一个**增量扩展**（多一列），原有五列语义完全保留。
        """
        with self._connect() as con:
            con.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE_NAME} USING fts5(
                    ticket_id,
                    description,
                    suggestion,
                    outcome,
                    metadata,
                    seg,
                    tokenize = 'unicode61'
                )
            """)
            con.commit()

    # ------------------------------------------------------------------
    # 写：沉淀一条案例
    # ------------------------------------------------------------------
    def add_case(self, ticket_id: str, description: str, suggestion: str,
                 outcome: str, metadata: Optional[dict] = None) -> None:
        """把一条已处理工单存进 FTS5 索引（幂等：同 ticket_id 覆盖）。

        参数：
            ticket_id:   工单编号（同时用作去重键）
            description: 问题描述（用户诉求原文）
            suggestion:  处理建议摘要
            outcome:     执行结果（一句人话，如"成功执行，客户满意"）
            metadata:    附加字段 dict（issue_type/store/customer/success），
                         内部 JSON 序列化后存进 metadata 列

        技术细节：
        - 先 DELETE 同 ticket_id 再 INSERT：FTS5 虚拟表没有主键约束，
          `INSERT OR REPLACE` 对它不生效（那是普通表的 ROWID 语义），
          所以用"删了再插"实现 upsert。真实场景里同一工单会被反复处理，
          不去重的话库里会堆出重复案例、污染检索排序。
        - 所有列都存字符串（FTS5 列是 TEXT 语义）。
        """
        if not self.available:
            return
        tid = str(ticket_id or "").strip()
        if not tid:
            log.warning("FTS：add_case 缺少 ticket_id，忽略")
            return
        desc = str(description or "")
        sug = str(suggestion or "")
        out = str(outcome or "")
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        # 检索字段 = 三段内容拼起来切词。把 suggestion/outcome 也纳入检索，
        # 这样"查被拦截的案例"这类按结果检索的诉求也能命中。
        seg = _segment(f"{desc} {sug} {out}")
        try:
            with self._connect() as con:
                con.execute(f"DELETE FROM {TABLE_NAME} WHERE ticket_id = ?", (tid,))
                con.execute(
                    f"INSERT INTO {TABLE_NAME} "
                    f"(ticket_id, description, suggestion, outcome, metadata, seg) "
                    f"VALUES (?, ?, ?, ?, ?, ?)",
                    (tid, desc, sug, out, meta_json, seg),
                )
                con.commit()
            log.info("FTS：已沉淀案例 %s（库中共 %d 条）", tid, self.count())
        except Exception as e:  # 沉淀失败不该影响主流程
            log.warning("FTS：沉淀案例失败(%s)，忽略: %s", type(e).__name__, e)

    # ------------------------------------------------------------------
    # 读：检索
    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int = 3) -> list[dict]:
        """按关键词召回历史案例，返回按相关度从高到低排序的列表。

        参数：
            query: 查询文本（通常是新工单的诉求原文）
            top_k: 返回条数上限

        返回：
            [{ticket_id, description, suggestion, outcome, score, metadata}, ...]
            score 是 [0,1] 的"相似度"（由 bm25 分数换算，越大越相关）。
            库不可用 / 查询为空 / 无命中 —— 一律返回空列表，不抛异常。

        技术细节：
        - MATCH 的查询串必须**经过同一个 _segment() 切词**，否则中文永远匹配不上
          （见模块头部"中文分词"说明）。
        - 查询串用 `OR` 连接"原查询 + 同义词扩展"：FTS5 的 OR 是**并集**语义，
          命中原词的案例自然排前面（bm25 分更高），扩展词只负责补齐召回。
        - bm25() 返回的是"越小越相关"的负分（越负越好），
          这里换算成 0~1 的正向分值再返回，避免调用方把排序搞反。
        - 用 `ORDER BY bm25(表名)` 而不是默认顺序：FTS5 不排序时返回的是
          rowid 顺序，与相关度无关。
        """
        if not self.available or not str(query or "").strip():
            return []
        # 主查询词 + 同义词扩展词（各自切词后，用 OR 拼成一个 MATCH 表达式）
        terms = [str(query)] + _expand_query(query)
        clauses: list[str] = []
        for t in terms:
            seg = _segment(t)
            if seg:
                # 用双引号包成"短语查询"：防用户输入里的 " 破坏 FTS 语法，
                # 同时要求 token 连续出现（bigram 已切好，正合适）
                clauses.append('"' + seg.replace('"', '""') + '"')
        if not clauses:
            # 查询串切完是空的（纯符号/纯空白）—— 不能拿它去 MATCH，会抛语法错
            return []
        match_expr = " OR ".join(clauses)
        try:
            total = self.count()
            if total <= 0:
                return []
            n = max(1, int(top_k))
            with self._connect() as con:
                rows = con.execute(
                    f"SELECT ticket_id, description, suggestion, outcome, metadata, "
                    f"bm25({TABLE_NAME}) AS rank "
                    f"FROM {TABLE_NAME} WHERE {TABLE_NAME} MATCH ? "
                    f"ORDER BY rank LIMIT ?",
                    (match_expr, n),
                ).fetchall()
        except Exception as e:
            log.warning("FTS：检索失败(%s)，按无历史案例处理: %s", type(e).__name__, e)
            return []

        hits: list[dict] = []
        for row in rows:
            hits.append({
                "ticket_id": row["ticket_id"],
                "description": row["description"],
                "suggestion": row["suggestion"],
                "outcome": row["outcome"],
                "metadata": self._loads_meta(row["metadata"]),
                "score": self._rank_to_score(row["rank"]),
            })
        return hits

    @staticmethod
    def _loads_meta(raw: Any) -> dict:
        """把 metadata 列的 JSON 字符串解回 dict（坏数据降级为空 dict）。"""
        if not raw:
            return {}
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _rank_to_score(rank: Any) -> float:
        """bm25 原始分 -> [0,1] 的相似度分值。

        技术细节：FTS5 的 bm25() 返回**负数**，越接近 0 越相关（0 最好）。
        不同查询之间绝对值不可比，所以这里做一个单调映射便于展示：
            相似度 = 1 / (1 + |bm25|)
        这样"完全命中"趋近 1、"弱命中"趋近 0，且严格单调 ——
        排序信息不丢，展示时也有个直观分数（等价于 sigmoid 形状的压缩）。
        """
        try:
            r = abs(float(rank))
        except (TypeError, ValueError):
            return 0.0
        return round(1.0 / (1.0 + r), 4)

    def count(self) -> int:
        """库中现有案例条数（不可用时返回 0）。"""
        if not self.available:
            return 0
        try:
            with self._connect() as con:
                return int(con.execute(
                    f"SELECT count(*) FROM {TABLE_NAME}").fetchone()[0])
        except Exception as e:  # pragma: no cover - 仅在库损坏时触发
            log.warning("FTS：读取案例数失败(%s)，按 0 处理: %s", type(e).__name__, e)
            return 0

    def reset(self) -> bool:
        """清空所有案例（演示/测试用）。成功返回 True。"""
        if not self.available:
            return False
        try:
            with self._connect() as con:
                con.execute(f"DELETE FROM {TABLE_NAME}")
                con.commit()
            log.info("FTS：案例库已清空")
            return True
        except Exception as e:
            log.warning("FTS：清空失败(%s): %s", type(e).__name__, e)
            return False

    def health(self) -> dict:
        """自检信息：可用性 + 案例数 + 库路径（给演示脚本和排查用）。"""
        return {
            "available": self.available,
            "reason": self.unavailable_reason,
            "db_path": self.db_path,
            "table": TABLE_NAME,
            "cases": self.count(),
        }


# ----------------------------------------------------------------------
# 全局单例
# 大白话：整个进程共用一个检索器（每次查询都重开库连接没必要）。
# 为什么用懒加载函数而不是模块级实例：
#   模块级实例会在 `import` 时就建表连库 —— 跑测试时会平白多一次磁盘 IO，
#   而且测试用 AGENT_DB_PATH 临时库时，实例可能已经绑在真实库上了。
#   懒加载让"什么时候连哪个库"变得可控（尤其是可注入路径的自检场景）。
# ----------------------------------------------------------------------
_default_store: Optional[FtsCaseStore] = None


def get_fts_store() -> FtsCaseStore:
    """获取全局 FTS 检索器单例（首次调用时才初始化）。

    库路径优先读 `AGENT_DB_PATH` 环境变量（与持久化层一致，测试隔离用），
    否则用默认的 data/agent_operations.db。

    技术细节：这里**每次调用都重新读一次** `AGENT_DB_PATH` ——
    早期实现是缓存 `src.persistence.database.DB_PATH`，但那个模块常量在
    `import` 时就定型了；若调用方在 import 之后才设置 AGENT_DB_PATH
    （测试里很常见），单例就会绑到旧库上，出现"写进去了却查不到"的怪现象。
    代价只是每次取单例时算一次路径，可以忽略。
    """
    global _default_store
    if _default_store is None:
        # 延迟导入：避免 `import src.memory.fts_store` 时就拉起持久化层
        from src.persistence.database import DB_PATH as _DB_PATH

        raw = os.getenv("AGENT_DB_PATH", "").strip()
        if raw:
            p = Path(raw)
            db_path = str(p if p.is_absolute() else (_project_root() / p).resolve())
        else:
            db_path = str(_DB_PATH)
        _default_store = FtsCaseStore(db_path=db_path)
    return _default_store


def _project_root() -> Path:
    """项目根目录（src/memory/fts_store.py 的上三级）。"""
    return Path(__file__).resolve().parent.parent.parent


def reset_fts_store() -> None:
    """丢弃全局单例（测试隔离 / 切换库路径时用）。

    大白话：下一次 get_fts_store() 会按当时的 AGENT_DB_PATH 重新连库。
    """
    global _default_store
    _default_store = None