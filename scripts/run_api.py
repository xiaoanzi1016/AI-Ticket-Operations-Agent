# -*- coding: utf-8 -*-
"""Phase 2 Web 服务启动脚本。

用法：
    python scripts/run_api.py                      # 默认 0.0.0.0:8000
    python scripts/run_api.py --port 9000
    python scripts/run_api.py --reload             # 开发模式：改代码自动重启
    python scripts/run_api.py --workers 2 --log-level warning

为什么不直接写 `uvicorn src.api.main:app`：
1. **sys.path**：uvicorn 从命令行启动时不保证项目根在 `sys.path` 里，
   `from src.xxx import ...` 会直接 ImportError。这里显式补上，避免"换个目录就起不来"。
2. **配置统一**：host/port/workers 默认值来自 `ApiSettings`（即 .env），
   命令行参数只做覆盖，不用在两处维护默认值。
3. **reload 的正确姿势**：`reload=True` 必须以**导入字符串**形式传 app
   （uvicorn 要另起子进程重新导入），传 app 对象是不生效的。这里已经处理好。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证能 import src（脚本在 scripts/ 下运行时，把项目根加入 sys.path）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api.settings import api_settings  # noqa: E402  (必须在 src.config 之前导入：见 settings.py 说明)

APP_IMPORT_STRING = "src.api.main:app"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数；默认值统一从 ApiSettings（.env）取。"""
    parser = argparse.ArgumentParser(
        description="启动智能工单售后 Agent 的 Web 服务",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=api_settings.api_host,
                        help="监听地址。0.0.0.0 = 容器/局域网内可访问")
    parser.add_argument("--port", type=int, default=api_settings.api_port,
                        help="监听端口")
    parser.add_argument("--reload", action="store_true", default=api_settings.api_reload,
                        help="开发模式：代码变更自动重启（会起一个监视进程，生产环境别开）")
    parser.add_argument("--workers", type=int, default=api_settings.workers,
                        help="后台 Worker 协程数。当前执行阶段是串行的，见 src/api/worker.py 说明")
    parser.add_argument("--log-level", default=api_settings.api_log_level,
                        choices=["critical", "error", "warning", "info", "debug", "trace"],
                        help="uvicorn 日志级别")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    # 只覆盖 Worker 并发数（uvicorn 的 --workers 是"多进程"，
    # 与本项目的"进程内后台消费协程数"不是一回事，这里刻意不暴露多进程，
    # 因为 SQLite 单文件库 + 全局单例 Agent 不支持多进程共享）。
    api_settings.workers = args.workers

    import uvicorn

    print("=" * 68)
    print("智能工单售后 Agent — Web 服务")
    print(f"  文档      : http://127.0.0.1:{args.port}/docs")
    print(f"  健康检查  : http://127.0.0.1:{args.port}/health")
    print(f"  提交任务  : POST http://127.0.0.1:{args.port}/api/v1/tasks/submit")
    print(f"  Worker    : {args.workers} 个（执行阶段串行）"
          f" | 强制 mock: {api_settings.agent_force_mock}")
    print(f"  上传目录  : {api_settings.upload_dir}")
    print("=" * 68)

    uvicorn.run(
        APP_IMPORT_STRING,          # 传字符串：reload 模式必须这么写
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
