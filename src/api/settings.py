# -*- coding: utf-8 -*-
"""API 层配置（PHASE 2 新增）。

【这个文件是干什么的】
把 Web 服务的所有可调参数集中到一处，全部可以用「环境变量 / .env 文件」覆盖：
监听地址、端口、Worker 数量、上传大小上限、临时目录、CORS 白名单、是否强制 mock 等。

【为什么用 pydantic-settings，而不是继续 os.getenv】
- 自动做类型转换与校验：`MAX_UPLOAD_SIZE=abc` 会在**启动时**就报错，
  而不是等到第一个上传请求把服务打崩；
- 一处声明、一处文档，配合 `.env.example` 就是完整的配置说明；
- 优先级清晰：真实环境变量 > .env 文件 > 代码默认值（测试用环境变量压盖最方便）。

【必须知道的一个兼容点：DEEPSEEK_* 与 MODEL_* 的关系】
本项目 Phase 1 的 `src/config.py` 读的是 `MODEL_API_KEY / MODEL_BASE_URL / MODEL_NAME`；
而 Phase 2 需求书给的变量名是 `DEEPSEEK_API_KEY / DEEPSEEK_MODEL`。
两套命名都支持 —— 本模块在**导入时**把 DEEPSEEK_* 回填进 MODEL_*，
且刻意早于 `src.config` 被导入（见 `src/api/__init__.py` 与 `scripts/run_api.py`），
这样无论用户按哪套命名填 .env，大模型都能正常读到 key。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（settings.py 位于 src/api/ 下，所以往上三层）
ROOT: Path = Path(__file__).resolve().parent.parent.parent
ENV_FILE: Path = ROOT / ".env"

# ----------------------------------------------------------------------
# 1) 别名回填（必须在 src.config 被导入之前执行）
# ----------------------------------------------------------------------
# 大白话：用户按需求书填了 DEEPSEEK_API_KEY，但项目代码找的是 MODEL_API_KEY。
#         这里先把两套名字对齐，谁也不用手工改代码。
# 技术细节：为什么用 dotenv_values 而不是 os.getenv？因为此刻 src.config 还没执行
#         load_dotenv，.env 里的值尚未进入 os.environ，必须自己先读一遍文件。
try:
    from dotenv import dotenv_values

    _FILE_ENV: dict[str, str] = {
        k: v for k, v in (dotenv_values(ENV_FILE) or {}).items() if v is not None
    }
except Exception:  # pragma: no cover - 未装 python-dotenv 或 .env 不可读时静默降级
    _FILE_ENV = {}


def _raw(name: str, default: str = "") -> str:
    """取配置值：真实环境变量优先，其次 .env 文件，最后默认值。"""
    return (os.getenv(name) or _FILE_ENV.get(name) or default).strip()


# DEEPSEEK_* -> MODEL_* 别名映射（只在目标变量为空时才回填，不覆盖用户显式配置）
_ALIASES: tuple[tuple[str, str], ...] = (
    ("DEEPSEEK_API_KEY", "MODEL_API_KEY"),
    ("DEEPSEEK_BASE_URL", "MODEL_BASE_URL"),
    ("DEEPSEEK_MODEL", "MODEL_NAME"),
)
for _alias, _target in _ALIASES:
    if _raw(_alias) and not _raw(_target):
        os.environ[_target] = _raw(_alias)


# ----------------------------------------------------------------------
# 2) 配置项
# ----------------------------------------------------------------------
class ApiSettings(BaseSettings):
    """Web 服务配置。字段名即环境变量名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",          # .env 里本模块不认识的键（如 MODEL_API_KEY）忽略即可
        case_sensitive=False,
    )

    # ---- 服务本身 ----
    api_title: str = "智能工单售后 Agent API"
    api_version: str = "1.0.0"
    api_host: str = "0.0.0.0"        # 0.0.0.0 = 容器/局域网内可访问
    api_port: int = 8000
    api_reload: bool = False         # 开发期热重载（生产必须关，reload 会起双进程）
    api_log_level: str = "info"

    # ---- 后台 Worker ----
    # 大白话：同时有几个"处理工单的柜员"。当前实现里 SQLite 是单文件库、
    #         Agent 也依赖若干全局单例，所以真正执行阶段是**串行**的；
    #         这个参数预留给后续换成 PostgreSQL + 无状态 Agent 时水平扩容。
    workers: int = 1
    max_queue_size: int = 1000       # 队列上限，满了直接 503，避免内存被无限堆满
    worker_poll_interval: float = 0.1

    # ---- 上传 ----
    max_upload_size: int = 10 * 1024 * 1024          # 单个文件上限 10MB
    allowed_upload_suffixes: str = ".xlsx,.xls,.csv"
    upload_temp_dir: str = ""       # 留空则用「系统临时目录/ai-ticket-agent」
    keep_upload_files: bool = False  # True = 处理完不删临时文件（排查问题用）

    # 是否允许"上传的数据文件真正参与本次查询"。
    # 关掉后上传文件只做接收与留痕，Agent 仍读 data/mock/ 下的默认数据源。
    use_uploaded_dataset: bool = True

    # ---- Agent 运行 ----
    # 强制 mock：不调用真实大模型（离线演示 / 自动化测试用）。
    agent_force_mock: bool = False

    # ---- CORS ----
    # 逗号分隔；"*" 表示允许任意来源（默认，方便后续接前端）
    cors_origins: str = "*"

    # ---- 派生属性 ----
    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的字符串拆成列表，供 CORSMiddleware 使用。"""
        items = [o.strip() for o in (self.cors_origins or "").split(",") if o.strip()]
        return items or ["*"]

    @property
    def allow_credentials(self) -> bool:
        """是否允许携带 Cookie。

        技术细节：CORS 规范禁止 `Access-Control-Allow-Origin: *` 与
        `Allow-Credentials: true` 同时出现，浏览器会直接拒绝。所以通配来源时必须关掉它。
        """
        return "*" not in self.cors_origin_list

    @property
    def allowed_suffix_set(self) -> set[str]:
        """允许上传的扩展名集合（统一小写）。"""
        return {s.strip().lower() for s in self.allowed_upload_suffixes.split(",") if s.strip()}

    @property
    def upload_dir(self) -> Path:
        """上传文件的落地根目录（每任务一个子目录）。

        大白话：文件先扔进系统临时目录，任务处理完就删掉，不给磁盘留垃圾。
        """
        base = Path(self.upload_temp_dir) if self.upload_temp_dir else (
            Path(tempfile.gettempdir()) / "ai-ticket-agent"
        )
        base.mkdir(parents=True, exist_ok=True)
        return base


# 全局唯一配置实例（与 Phase 1 的 src.config.settings 同风格：模块级单例 + 可注入）
api_settings = ApiSettings()
