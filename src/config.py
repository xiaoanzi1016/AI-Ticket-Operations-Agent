# -*- coding: utf-8 -*-
"""全局配置：优先读取环境变量 / .env 文件。"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

# 项目根目录（先于 load_dotenv 定义，用于定位 .env）
ROOT = Path(__file__).resolve().parent.parent


def _env_float(name: str, default: float) -> float:
    """读浮点型环境变量，值非法时回退默认值（并打一条 WARNING）。

    为什么不像其它字段那样直接 float(os.getenv(...))：阈值类配置是运维会手改的
    （.env 里把 0.4 写成 "0,4" 太常见了），裸 float() 会让**整个进程在 import
    阶段就起不来** —— 一个配置笔误换来服务不可用，代价完全不成比例。
    宁可回退默认值 + 留一条日志，也好过直接崩。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _log.warning("环境变量 %s=%r 不是合法数字，回退为 %r", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    """读整型环境变量，值非法时回退默认值（理由同 _env_float）。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _log.warning("环境变量 %s=%r 不是合法整数，回退为 %r", name, raw, default)
        return default

# 显式加载项目根目录的 .env（不依赖当前工作目录）
# override=True：.env 里的值优先于系统环境变量
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=True)
except ImportError:  # 未装 python-dotenv 时静默降级
    pass


# ---- 密钥占位值识别 ----------------------------------------------------
# 为什么需要：.env.example 里的占位值是 `sk-your-key-here`，而原来的判定
# 只排除了字面量 "sk-xxx"。照 README 执行 `cp .env.example .env` 之后，
# 占位值会被当成**已配置好的真密钥**，于是应用静默从 mock 演示模式切到
# 真实模型模式，每个任务都去调一个必然失败的模型、再被 fail-safe 降级成
# "转人工" —— 表现出来像"Agent 什么都不干只会转人工"，而且很难归因。
# 这里改成按特征子串匹配，覆盖常见的占位写法。
_PLACEHOLDER_KEY_HINTS: tuple[str, ...] = (
    "sk-xxx", "your-key", "your_key", "yourkey", "xxx", "changeme",
    "placeholder", "todo", "replace-me", "replace_me", "example", "fill",
)


def is_placeholder_key(key: Optional[str]) -> bool:
    """判断一个密钥是不是"还没填"的占位值（空值也算）。"""
    k = str(key or "").strip().lower()
    if not k:
        return True
    return any(hint in k for hint in _PLACEHOLDER_KEY_HINTS)


@dataclass
class Settings:
    """集中式配置，避免散落魔法数字。"""

    # ---- 模型 ----
    model_api_key: str = field(default_factory=lambda: os.getenv("MODEL_API_KEY", ""))
    model_base_url: str = field(
        default_factory=lambda: os.getenv("MODEL_BASE_URL", "https://api.deepseek.com")
    )
    model_name: str = field(default_factory=lambda: os.getenv("MODEL_NAME", "deepseek-chat"))
    model_temperature: float = 0.0

    # ---- 运行 ----
    max_rounds: int = 8          # Agent 每任务最多工具轮次
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "1800"))

    # ---- 路径 ----
    data_dir: Path = ROOT / "data"
    mock_dir: Path = ROOT / "data" / "mock"
    log_dir: Path = ROOT / "logs"
    output_dir: Path = ROOT / "outputs"

    # ---- 数据库 ----
    # 业务数据 + 运行台账共用的 SQLite 库文件。
    # 优先级：DATABASE_URL（sqlite:///... 形式，见 .env.example）> AGENT_DB_PATH
    #        > 默认 data/agent_operations.db。
    # 这里只负责"算出路径"，真正建引擎在 src/persistence/database.py 里做 ——
    # 配置层不碰 SQLAlchemy，避免没装 sqlalchemy 时连配置都导不进来。
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./data/agent_operations.db")
    )
    db_path: Path = field(init=False, default=ROOT / "data" / "agent_operations.db")

    # ---- 安全 ----
    refund_max_amount: float = 5000.0      # 单笔退款上限（超限走人工）
    reissue_max_qty: int = 10              # 单笔补发数量上限
    batch_action_limit: int = 20           # 单批操作上限（防批量误操作）

    # ---- RAG（历史工单案例检索）----
    # 大白话：让 Agent 处理新工单时先翻一翻"以前类似的单子是怎么结的"，
    #         把 top-k 条历史案例塞进 prompt，建议从规则版升级为案例驱动。
    # 实现是 SQLite FTS5 全文检索（src/memory/fts_store.py），零新依赖、零模型下载。
    # 这些都是"增强项"开关 —— rag_enabled=False 时行为与加 RAG 之前完全一致。
    rag_enabled: bool = field(
        default_factory=lambda: os.getenv("RAG_ENABLED", "true").strip().lower()
        not in ("0", "false", "no", "off")
    )
    rag_top_k: int = field(default_factory=lambda: int(os.getenv("RAG_TOP_K", "2")))

    # ---- 退货风险（业务规则）----
    # 大白话：客户老是退货，说明这里面可能有猫腻（恶意退货、商品本身有问题、
    #         或者地址/描述有系统性偏差）。这类工单不能让 Agent 一路自动处理，
    #         得"叫人来"。这里放的是判定用的开关与阈值。
    # 与 RAG 一样属于"增强项"：return_risk_enabled=False 时行为与加规则之前一致。
    return_risk_enabled: bool = field(
        default_factory=lambda: os.getenv("RETURN_RISK_ENABLED", "true").strip().lower()
        not in ("0", "false", "no", "off")
    )
    # 客户退货率阈值：达到即"升级人工"（不再走自动建议流程）。
    # 注意这是"业务参数"，标定得靠真实数据分布，不是拍脑袋 —— 见 README 的说明。
    return_risk_threshold_customer: float = field(
        default_factory=lambda: _env_float("RETURN_RISK_THRESHOLD_CUSTOMER", 0.4))
    # 单商品退货率阈值：达到即在建议里提示"该商品近期退货率高"，提醒人工验货。
    return_risk_threshold_sku: float = field(
        default_factory=lambda: _env_float("RETURN_RISK_THRESHOLD_SKU", 0.3))
    # 最小退货次数：近 30 天退货订单数达到这个数即标记"高风险"（未到率阈值时兜底）。
    return_risk_min_count: int = field(
        default_factory=lambda: _env_int("RETURN_RISK_MIN_COUNT", 2))

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.mock_dir, self.log_dir, self.output_dir):
            p.mkdir(parents=True, exist_ok=True)

    @property
    def has_model_key(self) -> bool:
        """是否配置了可用的模型密钥（占位值一律视为"没配"）。"""
        return not is_placeholder_key(self.model_api_key)


def _resolve_db_path(cfg: "Settings") -> Path:
    """把 DATABASE_URL / AGENT_DB_PATH 解析成绝对的库文件路径。

    支持的写法（按优先级）：
      1. 环境变量 AGENT_DB_PATH  —— 最优先，测试用它把库重定向到临时目录。
         注意：它是一个**文件路径**，不是 URL。
      2. cfg.database_url 形如 sqlite:///./data/agent_operations.db —— 去掉
         `sqlite:///` 前缀得到路径；相对路径按项目根目录解析（而不是进程 cwd，
         否则从别的目录启动就会连到另一个库上，非常难排查）。
      3. 都没有 → 默认 data/agent_operations.db。

    为什么不用 SQLAlchemy 的 make_url 来解析：配置层刻意不依赖 SQLAlchemy
    （见 database_url 字段注释），且 sqlite URL 的结构简单，手动拆更可控。
    """
    raw = os.getenv("AGENT_DB_PATH", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (ROOT / p).resolve()

    url = (cfg.database_url or "").strip()
    if url.startswith("sqlite:///"):
        tail = url[len("sqlite:///"):]
        p = Path(tail)
        return p if p.is_absolute() else (ROOT / p).resolve()
    return ROOT / "data" / "agent_operations.db"


# 全局唯一配置实例
settings = Settings()
settings.db_path = _resolve_db_path(settings)
settings.ensure_dirs()