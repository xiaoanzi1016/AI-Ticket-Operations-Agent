# Phase 2 Web 服务化交付说明

> 目标：把命令行里的 Agent 包装成 HTTP 服务，支持多人提交工单、查询结果、取消任务。
> 交付日期：2026-09-30
> 基线：Phase 1 已完成（SQLite 持久化 + `query_cli` 历史查询），本次在其之上叠加 Web 层。

---

## 0. 先说三处与需求书的偏差（请先读这一节）

### 偏差 1｜需求清单之外多做了 3 个文件：`settings.py` / `dataset.py` / `worker.py`

需求给的新增结构是 `__init__.py / main.py / models.py / deps.py / routers/*`。
我按同样风格多拆了 3 个模块，理由是**塞进 main.py 会让它膨胀到 600+ 行，职责也混了**：

| 文件 | 职责 | 不拆的话 |
|---|---|---|
| `src/api/settings.py` | pydantic-settings 配置 + `DEEPSEEK_*`→`MODEL_*` 别名映射 | 配置散落在 main/deps 里，别名映射没有明确的执行时机 |
| `src/api/dataset.py` | 上传文件 → 临时目录 → 任务级数据源 | "文件管理 + 全局状态临时替换"和 HTTP 路由混在一起 |
| `src/api/worker.py` | 异步队列 + 后台 Worker + 取消标志 | main.py 里要塞进队列、Worker、取消、终态回写 |

其余文件严格按需求给定的路径与顺序交付。另外补了 `.dockerignore`（理由见第 5 节）。

### 偏差 2｜`.env.example` 的变量名做了兼容处理

需求书给的样例是 `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL`，但**本项目 `src/config.py` 读的是
`MODEL_API_KEY` / `MODEL_NAME`**（Phase 1 起沿用）。直接用需求的名字，会导致模型 key 读不到、
服务静默降级成 mock。

做法：`src/api/settings.py` 在**导入时**把 `DEEPSEEK_*` 回填到 `MODEL_*`，
并保证它早于 `src.config` 被导入（`src/api/__init__.py` 与 `scripts/run_api.py` 都放在第一行导入）。
**两套命名都能用，空值不覆盖。** `.env.example` 里两者都列了，并注明只填一组即可。

### 偏差 3｜上传的三个字段只有 `delivery` 能驱动查询

需求给了 `delivery`（发货订单）/ `return`（退货订单）/ `warehouse`（仓库退货订单）三个字段，
但项目数据源里只有 `orders.csv` / `inventory.csv` / `returns.csv` 三个文件，**且没有库存上传字段**。
所以映射关系是：

| 表单字段 | 落到 | 效果 |
|---|---|---|
| `delivery` | `orders_file` | ✅ 本次任务真的用这份数据查单/查物流/判库存 |
| `return` | `returns_file` | 落到数据源，但**当前查询层还没有消费退货数据的入口**，等于只留痕 |
| `warehouse` | — | 数据源无对应文件，只做接收与留痕 |

这不是偷懒 —— 硬把"仓库退货单"塞进"库存文件"会得到语义错误的结果。
真要让它们生效，需要先给数据源补对应的查询接口（Phase 3）。

> 另外：需求里 `MAX_UPLOAD_SIZE` 的注释写作"数据源文件大小"，本实现按**单个上传文件上限**处理。

---

## 1. 交付物清单

**新增（Web 层）**

| 文件 | 说明 |
|---|---|
| `src/api/__init__.py` | 包出口 + 配置最先加载 |
| `src/api/settings.py` | pydantic-settings 配置（含 `DEEPSEEK_*` 别名映射） |
| `src/api/models.py` | Pydantic 请求/响应模型 + ORM→响应转换 |
| `src/api/deps.py` | 依赖注入：数据库会话 / Worker / 配置 |
| `src/api/dataset.py` | 上传文件落盘 → 转 CSV → 任务级数据源（用后还原） |
| `src/api/worker.py` | `asyncio.Queue` + 后台 Worker + 协作式取消 |
| `src/api/main.py` | FastAPI 应用：CORS / 全局异常处理 / lifespan |
| `src/api/routers/__init__.py` | 路由聚合 |
| `src/api/routers/system.py` | `/health`、`/metrics` |
| `src/api/routers/tasks.py` | `submit` / `recent` / `{task_id}` / `cancel` |
| `scripts/run_api.py` | Web 服务启动脚本（argparse） |
| `Dockerfile` / `docker-compose.yml` / `.dockerignore` / `.env.example` | 容器化与配置样例 |
| `tests/test_api.py` | 25 项接口测试 |

**修改**

| 文件 | 改动 |
|---|---|
| `src/agent.py` | `run()` 支持外部传入 task_id + 协作式取消（`# PHASE 2` 标注） |
| `src/persistence/crud.py` | 新增分页查询 `get_tasks_page` / `count_tasks` |
| `src/models/client.py` | **修复 Phase 1 遗留缺陷**：思考模式模型未回传 `reasoning_content`（见第 12 节） |
| `requirements.txt` | 追加 fastapi / uvicorn / python-multipart / pydantic-settings / httpx |
| `tests/conftest.py` | 追加 `AGENT_FORCE_MOCK=1`，测试不再误调真实大模型 |
| `.gitignore` | 修复 `.env.*` 把 `.env.example` 一起忽略的缺陷 |
| `README.md` | 增补 Web 服务章节 |

---

## 2. `requirements.txt`（新增部分）

```text
# ---- Web 服务层（Phase 2）----
# 把 Agent 包装成 HTTP 服务：提交任务 / 查询结果 / 取消任务
fastapi>=0.115.0                     # Web 框架（自带 OpenAPI 文档，见 /docs）
uvicorn[standard]>=0.30.0            # ASGI 服务器（standard 附带 uvloop/httptools/watchfiles）
python-multipart>=0.0.9              # FastAPI 解析 multipart/form-data —— 文件上传必需
pydantic-settings>=2.0.0             # 配置管理：环境变量 / .env + 类型校验

# ---- 测试 ----
# Phase 2 的接口测试用 fastapi.testclient，其底层依赖 httpx
httpx>=0.27.0
```

---

## 3. `.env.example`

```text
# ======================================================================
# 智能工单售后 Agent — 环境变量样例
# 用法：cp .env.example .env  然后按需修改
# 注意：.env 已加入 .gitignore，不要把真实 key 提交到版本库
# ======================================================================

# ----------------------------------------------------------------------
# 1. 大模型配置
# ----------------------------------------------------------------------
# 本项目 src/config.py 读取的变量名是 MODEL_*（Phase 1 起沿用），
# 而 Phase 2 需求书给的命名是 DEEPSEEK_*。两套都支持：
# src/api/settings.py 会把 DEEPSEEK_* 自动回填到 MODEL_*，
# 所以下面两组**只填其中一组**即可（两组都填时以 MODEL_* 为准）。
MODEL_API_KEY=sk-your-key-here
MODEL_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-v4-flash

# 等价别名（可选，填了上面就不用填）
# DEEPSEEK_API_KEY=sk-your-key-here
# DEEPSEEK_MODEL=deepseek-v4-flash

# 会话记忆 TTL（秒）：同一会话多久没说话就丢弃上下文
SESSION_TTL_SECONDS=1800

# ----------------------------------------------------------------------
# 2. 数据库
# ----------------------------------------------------------------------
# SQLite 库文件位置（Phase 1 起沿用）。
# 不填时默认：<项目根>/data/agent_operations.db（推荐，绝对路径，不受启动目录影响）。
# 若确实要指定，建议写绝对路径 —— 相对路径是相对"进程启动目录"解析的，
# 用 docker/systemd 启动时很容易踩坑。
# AGENT_DB_PATH=data/agent_operations.db

# ----------------------------------------------------------------------
# 3. Web 服务（Phase 2）
# ----------------------------------------------------------------------
API_HOST=0.0.0.0
API_PORT=8000
API_VERSION=1.0.0
# 开发期改代码自动重启；生产务必保持 false（reload 会多起一个监视进程）
API_RELOAD=false
API_LOG_LEVEL=info

# 后台 Worker 协程数。
# 注意：当前"真正执行 Agent"的阶段是串行的（全局单例 + SQLite 单文件所限），
# 详见 src/api/worker.py 顶部说明。这个值预留给后续接入 PostgreSQL 后水平扩容。
WORKERS=1
MAX_QUEUE_SIZE=1000

# 单个上传文件大小上限（字节）。默认 10MB = 10485760
MAX_UPLOAD_SIZE=10485760
# 允许的上传扩展名（逗号分隔）
ALLOWED_UPLOAD_SUFFIXES=.xlsx,.xls,.csv
# 上传文件临时目录；留空 = 系统临时目录下的 ai-ticket-agent/
# UPLOAD_TEMP_DIR=
# 设为 true 时任务结束不删临时文件（仅排查问题用，会导致磁盘堆积）
KEEP_UPLOAD_FILES=false
# 是否允许"上传的订单文件真正参与本次查询"；false 时上传只做接收与留痕
USE_UPLOADED_DATASET=true

# 强制 mock：不调真实大模型（离线演示 / 自动化测试）。生产请保持 false
AGENT_FORCE_MOCK=false

# CORS 允许的来源，逗号分隔；* 表示任意来源（方便接前端）
CORS_ORIGINS=*
```

---

## 4. `Dockerfile`

```dockerfile
# ======================================================================
# 智能工单售后 Agent — Web 服务镜像（Phase 2）
#
# 构建： docker build -t ai-ticket-operations-agent:phase2 .
# 运行： docker run -p 8000:8000 --env-file .env ai-ticket-operations-agent:phase2
# 一键： docker compose up --build
# ======================================================================
# 使用 dockerfile:1 前端语法（BuildKit），获得更好的缓存与并行能力
# syntax=docker/dockerfile:1

# 基础镜像：与项目开发环境（Python 3.14.7）保持同一大版本。
# 用 -slim 而不是完整版：体积从 ~1GB 降到 ~150MB，且本服务不需要编译工具链
# （pandas/numpy/openpyxl 都有预编译 wheel）。
FROM python:3.14-slim

# ---- 运行环境基础设置 -------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# 大白话：/app 就是容器里的"项目目录"，后面所有命令都在这儿执行
WORKDIR /app

# ---- 系统依赖 ---------------------------------------------------------
# libgomp1：pandas / numpy 依赖的 OpenMP 运行时。slim 镜像里没有，
#           缺了会在 `import pandas` 时报 "libgomp.so.1: cannot open shared object file"。
# curl    ：给下面的 HEALTHCHECK 用。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# ---- 依赖层（利用 Docker 层缓存）--------------------------------------
# 关键顺序：先只复制 requirements.txt 并安装，再复制源码。
# 这样日常改代码时，只要 requirements.txt 没变，这一层直接命中缓存，
# 重建只需几秒 —— 否则每次都要重装 pandas（几分钟）。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ---- 业务代码 ---------------------------------------------------------
# 注意：.dockerignore 里排除了 .venv/、data/*.db、logs/ 等，
# 否则会把宿主机的 Windows 虚拟环境和历史数据库一起塞进镜像。
COPY . .

# 运行期需要写入的目录（数据库 / 日志 / 评测报告）。
# 镜像里先建好，即使不挂卷也能跑起来。
RUN mkdir -p /app/data /app/logs /app/outputs

# ---- 运行时 -----------------------------------------------------------
EXPOSE 8000

# 容器健康状态：直接复用 /health 接口 —— 它内部还会探一次数据库，
# 所以"健康"的含义是"能对外服务"，而不只是"进程还活着"。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 生产启动命令：单进程 uvicorn。
# 不开 --reload（开发用）、不开 --workers N（多进程会让每个进程各有一份
# SQLite 连接与内存队列，任务状态会互相看不见；横向扩容见 README 说明）。
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ----------------------------------------------------------------------
# 生产加固（可选，默认未启用）：
# 上面的容器以 root 运行，好处是绑定挂载宿主机目录时不会遇到权限问题
# （Windows / Docker Desktop 上尤其省事）。若部署到 Linux 生产环境，
# 建议取消下面几行的注释，用非 root 用户运行：
#
# RUN useradd --create-home --uid 10001 appuser \
#     && chown -R appuser:appuser /app
# USER appuser
# ----------------------------------------------------------------------
```

---

## 5. `docker-compose.yml`

```yaml
# ======================================================================
# 智能工单售后 Agent — docker compose 编排（Phase 2）
#
# 启动： docker compose up --build
# 停止： docker compose down
# 日志： docker compose logs -f api
#
# 说明：**单服务即可**。SQLite 是文件型数据库，不需要单独的数据库容器；
#       后台任务队列是进程内的 asyncio.Queue，也不需要 Redis。
# ======================================================================
services:
  api:
    build:
      context: .
      dockerfile: Dockerfile
    image: ai-ticket-operations-agent:phase2
    container_name: ai-ticket-agent-api

    # ---- 端口映射：宿主机 8000 -> 容器 8000 ----
    # 左边可用 .env 里的 API_PORT 覆盖（compose 会自动加载同目录的 .env 做变量替换）
    ports:
      - "${API_PORT:-8000}:8000"

    # ---- 环境变量 ----
    # 这里用 ${VAR:-默认值} 的写法而不是 env_file：
    # compose 本来就会自动加载同目录的 .env 做变量替换，所以两种效果一样；
    # 但这种方式在 .env 不存在时也能正常启动（走默认值），不会因为缺文件直接报错。
    # 好处还有：一眼能看清"容器到底会拿到哪些配置"，不用去翻 .env。
    environment:
      # 模型：本项目读 MODEL_*；只填了 DEEPSEEK_* 也行（src/api/settings.py 会自动映射）
      MODEL_API_KEY: "${MODEL_API_KEY:-}"
      MODEL_BASE_URL: "${MODEL_BASE_URL:-https://api.deepseek.com}"
      MODEL_NAME: "${MODEL_NAME:-deepseek-v4-flash}"
      DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY:-}"
      DEEPSEEK_MODEL: "${DEEPSEEK_MODEL:-}"
      SESSION_TTL_SECONDS: "${SESSION_TTL_SECONDS:-1800}"
      # 数据库固定在挂载出来的 data/ 目录，做到"容器删了重建，审计记录还在"
      AGENT_DB_PATH: "/app/data/agent_operations.db"
      # 服务与队列
      WORKERS: "${WORKERS:-1}"
      MAX_QUEUE_SIZE: "${MAX_QUEUE_SIZE:-1000}"
      # 上传
      MAX_UPLOAD_SIZE: "${MAX_UPLOAD_SIZE:-10485760}"
      ALLOWED_UPLOAD_SUFFIXES: "${ALLOWED_UPLOAD_SUFFIXES:-.xlsx,.xls,.csv}"
      USE_UPLOADED_DATASET: "${USE_UPLOADED_DATASET:-true}"
      KEEP_UPLOAD_FILES: "${KEEP_UPLOAD_FILES:-false}"
      # 调试开关：设成 true 就完全不调大模型，适合离线演示
      AGENT_FORCE_MOCK: "${AGENT_FORCE_MOCK:-false}"
      CORS_ORIGINS: "${CORS_ORIGINS:-*}"
      # 时区：用环境变量而不是 compose 的 tz 键 —— 老版本 compose 不认 tz，
      # 会直接报 "Additional property tz is not allowed" 导致起不来。
      # 日志时间与本地一致才方便排查。
      TZ: "${TZ:-Asia/Shanghai}"

    # ---- 卷挂载（开发模式）----
    # 把本地代码挂进容器，配合 --reload 做到"改完代码立刻生效，不用重建镜像"。
    # data/ 必须挂出来：否则容器一删，SQLite 里的审计记录就没了。
    volumes:
      - ./src:/app/src                      # 业务代码（含 src/api）
      - ./scripts:/app/scripts              # 命令行/启动脚本
      - ./tests:/app/tests                  # 测试（可在容器里跑 pytest）
      - ./data:/app/data                    # 数据库 + data/mock 源数据
      - ./logs:/app/logs                    # 日志
      - ./outputs:/app/outputs              # 评测/报告产物
      - ./requirements.txt:/app/requirements.txt:ro

    # 开发模式启动命令：开启热重载。
    # 生产部署请删除这一行，改用镜像内置的 CMD（不带 --reload）。
    command:
      ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s

    restart: unless-stopped
```

> 场景说明：挂载 `./src` 等目录 + `--reload` 是**开发模式**（改代码即时生效）。
> 生产部署请删掉 `command` 那一行，用镜像内置的 CMD（不带 reload）。

---

## 6. `scripts/run_api.py`

```python
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
```

---

## 7. `src/api/__init__.py`

```python
# -*- coding: utf-8 -*-
"""Web 服务层（PHASE 2）。

【这一层是干什么的】
把原本只能在命令行里跑的 Agent 包装成 HTTP 服务，让多人/多系统都能提交工单、
查询处理结果。它**不重新实现业务逻辑**，只是给 Phase 1 的 Agent 加一层"接入壳"：
  路由接收请求 → 上传文件落临时目录 → 建任务记录 → 丢进异步队列 → 立刻返回 task_id
  后台 Worker 从队列取任务 → 线程池里跑 Agent.run() → 回写任务状态

【目录结构】
  settings.py   配置（pydantic-settings）
  models.py     Pydantic 请求/响应模型
  deps.py       依赖注入（数据库会话 / 后台 Worker）
  dataset.py    上传文件 → 临时目录 → 任务级数据源
  worker.py     异步队列 + 后台 Worker + 取消标志
  main.py       FastAPI 应用实例（CORS / 异常处理 / lifespan）
  routers/      system.py（健康检查）、tasks.py（任务增查与取消）

【为什么这里要先导入 settings】
settings.py 在导入时会把 DEEPSEEK_* 别名回填成 MODEL_*（本项目 config.py 读的名字）。
必须保证它早于 src.config 被导入，否则大模型读取 key 时拿到的是空值。
放在包 __init__ 里是最省心、也最不容易被漏掉的做法。
"""
from src.api.settings import api_settings as api_settings  # noqa: F401  (导入即完成别名回填)

__all__ = ["api_settings"]
```

---

## 8. `src/api/main.py`

```python
# -*- coding: utf-8 -*-
"""FastAPI 应用入口（PHASE 2）。

启动方式：
    uvicorn src.api.main:app --host 0.0.0.0 --port 8000
    python scripts/run_api.py                 # 等价，且支持 --reload / --port 等参数

【这个文件负责什么】
只做"应用装配"，不写业务逻辑：
1. 建 FastAPI 实例（标题/版本/描述 → 自动生成 /docs）
2. 装 CORS 中间件（方便后续接前端）
3. 挂载两个路由模块（system 不带前缀、tasks 挂 /api/v1）
4. 注册全局异常处理器（保证任何错误都是统一的 JSON 形状）
5. 用 lifespan 管生命周期：启动时建表 + 拉起后台 Worker，关闭时优雅停掉

【为什么用 lifespan 而不是 @app.on_event("startup")】
`on_event` 在 FastAPI 里已标记为过时；lifespan 是标准的上下文管理器写法，
`yield` 之前是启动、之后是关闭，资源成对出现，不容易漏（漏关 Worker 会让
进程无法正常退出）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.deps import get_worker
from src.api.models import ErrorResponse
from src.api.routers import system, tasks
from src.api.settings import api_settings
from src.logger import log
from src.persistence.database import DB_PATH, init_db

# HTTP 状态码 -> 默认错误码（调用方没显式给 code 时用它兜底）
_DEFAULT_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    500: "internal_error",
    503: "service_unavailable",
}


def _error_payload(code: str, message: str) -> dict[str, Any]:
    """统一错误响应体。所有出错路径都套这一个壳，前端只需写一份错误处理。"""
    return {"error": {"code": code, "message": message}}


# ----------------------------------------------------------------------
# 生命周期
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用启停钩子。

    大白话：开门营业前把账本备好（建表）、把柜员叫上岗（Worker）；
            打烊时先让柜员把手上的活收尾（停 Worker），再关灯。
    """
    init_db()
    log.info("API 启动中：db=%s", DB_PATH)
    worker = get_worker()
    await worker.start()
    log.info("API 已就绪：workers=%d mock=%s", api_settings.workers,
             api_settings.agent_force_mock)
    try:
        yield
    finally:
        await worker.stop()
        log.info("API 已关闭")


# ----------------------------------------------------------------------
# 应用实例
# ----------------------------------------------------------------------
app = FastAPI(
    title=api_settings.api_title,
    version=api_settings.api_version,
    description=(
        "把「智能工单售后 Agent」包装成 HTTP 服务。\n\n"
        "**处理是异步的**：`POST /api/v1/tasks/submit` 只负责收单并返回 `task_id`，"
        "真正的 Agent 处理在后台队列里进行，用 `GET /api/v1/tasks/{task_id}` 轮询结果。"
    ),
    lifespan=lifespan,
)

# CORS：允许跨域，方便后续接前端页面或第三方系统。
# 注意 allow_credentials 与 "*" 不能同时开（浏览器会直接拒绝），见 ApiSettings 里的说明。
app.add_middleware(
    CORSMiddleware,
    allow_origins=api_settings.cors_origin_list,
    allow_credentials=api_settings.allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 路由：system 不带版本前缀（探活地址要稳定），tasks 挂到 /api/v1
app.include_router(system.router)
app.include_router(tasks.router, prefix="/api/v1")


@app.get("/", include_in_schema=False)
def root() -> dict[str, Any]:
    """根路径：给个"这是哪儿、去哪儿看文档"的提示。"""
    return {
        "service": api_settings.api_title,
        "version": api_settings.api_version,
        "docs": "/docs",
        "health": "/health",
        "submit": "POST /api/v1/tasks/submit",
    }


# ----------------------------------------------------------------------
# 全局异常处理：保证"任何错误都是同一个 JSON 形状"
# ----------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request,
                                 exc: StarletteHTTPException) -> JSONResponse:
    """处理主动抛出的 HTTPException（404 任务不存在、409 不能取消等）。

    大白话：业务代码只管抛"错在哪儿"，由这里统一翻译成对外的报文格式。
    """
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code") or _DEFAULT_CODES.get(exc.status_code, "http_error"))
        message = str(detail.get("message") or detail)
    else:
        code = _DEFAULT_CODES.get(exc.status_code, "http_error")
        message = str(detail)
    return JSONResponse(status_code=exc.status_code,
                        content=_error_payload(code, message),
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request,
                                       exc: RequestValidationError) -> JSONResponse:
    """处理入参校验失败（缺字段、类型不对、超出范围）。

    大白话：把 pydantic 那一长串英文报错，压成一句能看懂的"哪个字段错在哪"。
    """
    problems: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        problems.append(f"{loc or '请求体'}: {err.get('msg')}")
    return JSONResponse(
        status_code=422,
        content=_error_payload("validation_error", "；".join(problems) or "请求参数不合法"),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：任何没被处理的异常。

    大白话：内部到底怎么炸的写进日志给运维看，对外只回一句"服务器内部错误" ——
    堆栈信息里可能有路径、配置等敏感内容，不该返回给调用方。
    """
    log.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=_error_payload("internal_error",
                               "服务器内部错误，请稍后重试或联系管理员"),
    )


__all__ = ["app"]
```

---

## 9. `src/api/models.py`

```python
# -*- coding: utf-8 -*-
"""API 请求 / 响应模型（Pydantic v2）。

【这个文件是干什么的】
定义 HTTP 接口"进出长什么样"。它有三个作用：
1. **校验**：进来的字段类型/长度不对，FastAPI 直接返回 422，不会把脏数据带进业务层；
2. **裁剪**：出去的数据只暴露该暴露的字段（比如 ORM 里的自增 id 就不需要全给前端）；
3. **文档**：`/docs` 里的字段说明、示例，全部由这里的 Field 描述自动生成。

【和 ORM 模型的关系】
`src/persistence/models.py` 里的 Task / ToolExecution 是**数据库表**（怎么存）；
这里的 *Response 是**HTTP 报文**（怎么传）。两者刻意分开：
以后改表结构不会连带把接口协议改掉，反之亦然。
转换逻辑统一收在本文件底部的 `from_task / from_execution`，别处不再手写映射。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.persistence.models import Task, ToolExecution


# ----------------------------------------------------------------------
# 内部小工具
# ----------------------------------------------------------------------
def _parse_json(raw: Optional[str]) -> Optional[dict]:
    """把库里存的 JSON 字符串还原成对象。

    大白话：数据库里 input_params 存的是一行文字，接口上要还给前端一个真正的对象。

    技术细节：Phase 1 刻意采用"超长截断在字符串层"的策略，所以存下来的内容
    **不保证仍是合法 JSON**。这里解析失败不报错，而是降级成 `{"_raw": 原文}` ——
    审计场景下"能看到前 10000 字"比"因为格式坏了整条记录取不出来"重要得多。
    """
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_value": value}


# ----------------------------------------------------------------------
# 请求模型
# ----------------------------------------------------------------------
class TaskSubmitRequest(BaseModel):
    """提交任务时的**文本字段**。

    为什么附件不在这里：multipart 里的文件由 FastAPI 的 `UploadFile` 直接接收
    （Pydantic 模型不参与文件流的解析）。本模型只负责文本字段的校验，
    在 `routers/tasks.py` 的 submit 里构造出来后即完成"入参体检"。
    """

    user_input: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="自然语言任务描述，如「客户反映订单 PO20260928-00001 少发一瓶玻璃水，要求补发」",
    )
    customer: str = Field(
        "访客",
        max_length=64,
        description="客户名 / 会话隔离键。同一客户的历史对话会被复用，不同客户互不串味",
    )


# ----------------------------------------------------------------------
# 响应模型
# ----------------------------------------------------------------------
class TaskResponse(BaseModel):
    """提交任务后的即时回执（此时任务只在队列里，还没开始处理）。"""

    task_id: str = Field(..., description="任务编号，后续查询详情都用它")
    status: str = Field(..., description="任务状态：processing / completed / failed / cancelled")
    created_at: datetime = Field(..., description="任务创建时间（ISO 8601）")
    message: str = Field(..., description="给用户的提示文案")


class ExecutionItem(BaseModel):
    """一次工具执行 / 安全闸门判定的流水。

    注意 status 有三种取值，其中 blocked 是"被安全闸门拦下"，
    它同样是有效结果 —— 审计最关心的恰恰是"当时为什么没做"。
    """

    id: int
    tool_name: str = Field(..., description="工具名，如 query_order；闸门判定统一为 safety_gate")
    status: str = Field(..., description="success / failed / blocked")
    executed_at: datetime
    input_params: Optional[dict] = Field(None, description="入参（JSON 还原；截断时降级为 _raw）")
    output_result: Optional[dict] = Field(None, description="出参（同上）")
    error_message: Optional[str] = Field(None, description="失败或被拦截的原因")


class TaskDetailResponse(BaseModel):
    """任务详情：任务本身 + 完整的执行链路。"""

    task_id: str
    user_input: str
    status: str
    created_at: datetime
    updated_at: datetime
    result_summary: Optional[str] = Field(
        None,
        description="结果摘要。库里存的是 JSON 字符串，这里按原样返回，"
                    "方便前端自行决定是解析展示还是直接显示。",
    )
    executions: list[ExecutionItem] = Field(
        default_factory=list, description="该任务的全部工具执行流水（按时间正序 = 当时的执行链路）"
    )


class TaskListItem(BaseModel):
    """列表里的一条任务（只给概览字段，不带流水，避免响应体过大）。"""

    task_id: str
    user_input: str
    status: str
    created_at: datetime


class TaskListResponse(BaseModel):
    """分页列表响应。"""

    total: int = Field(..., description="任务总数，前端据此算总页数")
    limit: int = Field(..., description="本页请求的条数")
    offset: int = Field(..., description="本页跳过的条数")
    items: list[TaskListItem] = Field(default_factory=list)


class CancelResponse(BaseModel):
    """取消任务的回执。"""

    task_id: str
    status: str = Field(..., description="取消后的目标状态：cancelled")
    message: str = Field(..., description="说明文案。若任务已在执行中，会提示取消将在下一步生效")


class HealthResponse(BaseModel):
    """健康检查响应。"""

    status: str = Field(..., description="healthy / degraded")
    version: str


class MetricsResponse(BaseModel):
    """运行指标（给运维/演示看，不做鉴权）。"""

    queue_size: int = Field(..., description="还在队列里排队、尚未开始处理的任务数")
    running: int = Field(..., description="正在执行的任务数")
    workers: int = Field(..., description="后台 Worker 协程数量")
    worker_alive: bool = Field(..., description="Worker 是否在运行")
    today: dict[str, Any] = Field(default_factory=dict, description="当日处理量统计")


class ErrorDetail(BaseModel):
    code: str = Field(..., description="机器可读的错误码，如 task_not_found")
    message: str = Field(..., description="给人看的错误说明")


class ErrorResponse(BaseModel):
    """全局统一错误格式：任何接口出错，响应体都是这个形状。"""

    error: ErrorDetail


# ----------------------------------------------------------------------
# ORM -> 响应模型 的转换
# ----------------------------------------------------------------------
def from_execution(exe: ToolExecution) -> ExecutionItem:
    """把一条工具流水 ORM 对象转成响应模型。"""
    return ExecutionItem(
        id=exe.id,
        tool_name=exe.tool_name,
        status=exe.status,
        executed_at=exe.executed_at,
        input_params=_parse_json(exe.input_params),
        output_result=_parse_json(exe.output_result),
        error_message=exe.error_message,
    )


def from_task(task: Task, executions: Optional[list[ToolExecution]] = None) -> TaskDetailResponse:
    """把任务 ORM 对象（可选带上流水）转成详情响应模型。"""
    return TaskDetailResponse(
        task_id=task.task_id,
        user_input=task.user_input,
        status=task.status,
        created_at=task.created_at,
        updated_at=task.updated_at,
        result_summary=task.result_summary,
        executions=[from_execution(e) for e in (executions or [])],
    )


def to_list_item(task: Task) -> TaskListItem:
    """把任务 ORM 对象转成列表项。"""
    return TaskListItem(
        task_id=task.task_id,
        user_input=task.user_input,
        status=task.status,
        created_at=task.created_at,
    )
```

---

## 10. `src/api/deps.py`

```python
# -*- coding: utf-8 -*-
"""依赖注入（PHASE 2 新增）。

【这个文件是干什么的】
FastAPI 的 `Depends(...)` 机制需要"提供者函数"。这里集中放三个：
- `get_db()`      —— 给路由一个数据库会话，请求结束自动提交/回滚/关闭
- `get_worker()`  —— 给路由后台任务队列
- `get_settings()`—— 给路由读取当前配置

【为什么要走依赖注入，而不是在路由里直接 import 全局对象】
1. **测试可替换**：`app.dependency_overrides[get_db] = fake_db` 一行就能把
   真实数据库换成测试库，不用改业务代码；
2. **生命周期正确**：FastAPI 会在响应结束后自动执行生成器的收尾逻辑，
   保证会话一定被 close（漏 close 会把连接池占满）；
3. **显式声明依赖**：看函数签名就知道这个接口需要什么，比翻全局变量清楚。
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from src.api.settings import ApiSettings, api_settings
from src.api.worker import TaskWorker, task_worker
from src.persistence.database import get_db as _session_scope


def get_settings() -> ApiSettings:
    """提供当前生效的 API 配置。"""
    return api_settings


def get_db() -> Iterator[Session]:
    """提供数据库会话（FastAPI 依赖）。

    大白话：把一个请求要用到的数据库连接交出去，请求处理完自动归还。

    技术细节：这里刻意**沿用 Phase 1 `get_db()` 的上下文管理器模式**，
    只是把它改造成 FastAPI 需要的「生成器依赖」形状：
    yield 之前是准备，yield 之后的代码由框架在响应结束后执行。
    所以 Phase 1 那套 "正常 commit / 异常 rollback / 一定 close" 的语义完全保留。

    用法：
        def endpoint(db: Session = Depends(get_db)):
            ...
    """
    with _session_scope() as db:
        yield db


def get_worker() -> TaskWorker:
    """提供后台任务队列（Worker）。"""
    return task_worker
```

---

## 11. `src/api/routers/__init__.py` 与 `system.py`

```python
# -*- coding: utf-8 -*-
"""路由层包。"""
from src.api.routers import system as system  # noqa: F401
from src.api.routers import tasks as tasks  # noqa: F401

__all__ = ["system", "tasks"]
```

```python
# -*- coding: utf-8 -*-
"""系统路由：健康检查与运行指标（不放在 /api/v1 下）。

大白话：这两个接口是给"运维"看的，不是给业务用的 ——
        - /health  回答"服务还活着吗"，给 K8s / docker healthcheck / 负载均衡用；
        - /metrics 回答"现在忙不忙"，给演示和排查用。
所以它们不带版本号前缀：探活地址应当稳定，不该随 API 版本变化。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.api.deps import get_db, get_settings, get_worker
from src.api.models import HealthResponse, MetricsResponse
from src.api.settings import ApiSettings
from src.api.worker import TaskWorker
from src.logger import log
from src.persistence import crud

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="健康检查")
def health(db: Session = Depends(get_db),
           cfg: ApiSettings = Depends(get_settings)) -> HealthResponse:
    """存活探针。

    大白话：不光回一句"我还活着"，还顺手摸一下数据库 —— 数据库连不上时服务
    其实是不可用的，回 healthy 会把流量引进一个处理不了请求的进程。

    技术细节：`SELECT 1` 是最轻量的"连接是否可用"验证，不读任何业务表。
    探活失败**不抛异常**（抛了调用方拿到的是 500，语义上不好区分"服务挂了"和
    "接口报错"），而是返回 status="degraded"，由编排系统按需处理。
    """
    try:
        db.execute(text("SELECT 1"))
        status = "healthy"
    except Exception as e:  # pragma: no cover - 只有库挂了才会走到
        log.error("健康检查：数据库不可用: %s", e)
        status = "degraded"
    return HealthResponse(status=status, version=cfg.api_version)


@router.get("/metrics", response_model=MetricsResponse, summary="运行指标")
def metrics(db: Session = Depends(get_db),
            worker: TaskWorker = Depends(get_worker)) -> MetricsResponse:
    """队列与处理量指标。

    大白话：看一眼"队列里堆了多少单、手上有几单在处理、今天处理了多少"。
    """
    stats = worker.stats
    try:
        today = crud.get_stats_today(db)
    except Exception as e:  # pragma: no cover - 统计失败不该让指标接口 500
        log.warning("指标：读取当日统计失败: %s", e)
        today = {}
    return MetricsResponse(today=today, **stats)
```

---

## 12. `src/api/routers/tasks.py`

```python
# -*- coding: utf-8 -*-
"""任务路由：提交 / 查询详情 / 列表 / 取消。

【这个文件是干什么的】
对外提供 4 个业务接口，是"人"和"Agent"之间的接头处：

  POST /api/v1/tasks/submit           提交任务（可选带 Excel 附件）→ 立刻返回 task_id
  GET  /api/v1/tasks/recent           分页看最近任务
  GET  /api/v1/tasks/{task_id}        看某个任务的完整执行链路
  POST /api/v1/tasks/{task_id}/cancel 取消任务

【核心设计：接口只"接单"，不"做菜"】
提交接口绝不在这里跑 Agent —— 那会把事件循环卡死几十秒。它只做四件事：
落文件 → 建任务记录（processing）→ 丢进队列 → 返回 task_id。
真正的处理由 `worker.py` 的后台协程在线程池里完成，状态从库里的记录读。

【路由顺序有个坑】
`/recent` 必须写在 `/{task_id}` **前面**。FastAPI 按注册顺序匹配，
否则 `/recent` 会被 `/{task_id}` 抢先匹配，被当成 task_id="recent" 去查库。
"""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import ValidationError
from sqlalchemy.orm import Session

from src.api.dataset import UploadRejected, cleanup, save_uploads
from src.api.deps import get_db, get_worker
from src.api.models import (
    CancelResponse,
    TaskDetailResponse,
    TaskListResponse,
    TaskResponse,
    TaskSubmitRequest,
    from_task,
    to_list_item,
)
from src.api.worker import TaskJob, TaskWorker
from src.logger import log
from src.persistence import crud

router = APIRouter(prefix="/tasks", tags=["tasks"])

# 任务已结束（终态）的三种状态：不能再取消，也不会再变化
_TERMINAL_STATUSES: set[str] = {"completed", "failed", "cancelled"}


def _bad_request(code: str, message: str) -> HTTPException:
    """构造一个携带机器可读错误码的 4xx 异常。

    技术细节：detail 用 dict 而不是字符串，是为了让全局异常处理器能原样保留
    code —— 前端可以按 code 做分支（如 code="payload_too_large" 就提示用户压缩文件），
    而不是去正则匹配中文提示语。
    """
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                         detail={"code": code, "message": message})


# ----------------------------------------------------------------------
# 1) 提交任务
# ----------------------------------------------------------------------
@router.post("/submit", response_model=TaskResponse, summary="提交任务（异步处理）")
async def submit_task(
    user_input: Annotated[str, Form(description="自然语言任务描述")],
    customer: Annotated[str, Form(description="客户名 / 会话隔离键")] = "访客",
    delivery: Annotated[UploadFile | None,
                        File(description="发货订单 Excel（可选，会驱动本次查询）")] = None,
    # 注意：表单字段名必须叫 return，但 return 是 Python 关键字，不能当参数名，
    # 所以参数叫 return_order、用 alias 把表单字段名对齐回 return。
    return_order: Annotated[UploadFile | None,
                            File(alias="return", description="退货订单 Excel（可选）")] = None,
    warehouse: Annotated[UploadFile | None,
                         File(description="仓库退货订单 Excel（可选）")] = None,
    db: Session = Depends(get_db),
    worker: TaskWorker = Depends(get_worker),
) -> TaskResponse:
    """提交一条工单诉求，立刻返回任务编号（真正的处理在后台）。

    大白话：这叫"取号排队"—— 你把诉求和材料交上来，我回你一个号，
    你随时可以用这个号来查进度，不用在窗口干等。
    """
    # ① 文本字段体检（长度/非空由 pydantic 负责，出错转成 400 而不是 500）
    try:
        payload = TaskSubmitRequest(user_input=user_input, customer=customer)
    except ValidationError as e:
        first = e.errors()[0]
        raise _bad_request("invalid_field",
                           f"字段 {first.get('loc')} 不合法：{first.get('msg')}") from e

    # ② 附件落盘（体积/类型/格式问题在这里一次性拦掉）
    dataset = None
    try:
        dataset = await save_uploads(delivery, return_order, warehouse)
    except UploadRejected as e:
        raise HTTPException(status_code=e.status_code,
                            detail={"code": e.code, "message": str(e)}) from e

    # ③ 建任务记录：先落库再入队，保证"只要返回了 task_id，就一定查得到"
    try:
        task = crud.create_task(db, payload.user_input)
        if dataset is not None:
            # 附件也留一笔流水：审计要能回答"这一单当时带了什么材料"
            crud.create_tool_execution(
                db,
                task_id=task.task_id,
                tool_name="file_upload",
                input_params={"files": [f.to_dict() for f in dataset.files]},
                status="success",
                output_result={"drives_query": dataset.drives_query,
                               "note": "上传文件仅本次任务生效，处理完成后自动清理"},
            )
    except Exception as e:
        cleanup(dataset)   # 任务没建成，材料别留在磁盘上
        log.exception("提交任务：建任务记录失败")
        raise HTTPException(status_code=500,
                            detail={"code": "task_create_failed",
                                    "message": f"创建任务失败：{e}"}) from e

    # ④ 入队。队列满了就明确回 503（让调用方稍后重试），而不是无限堆内存
    job = TaskJob(task_id=task.task_id, user_input=payload.user_input,
                  customer=payload.customer, dataset=dataset)
    try:
        await worker.submit(job)
    except asyncio.QueueFull:
        cleanup(dataset)
        crud.update_task_status(db, task.task_id, "failed",
                                result_summary={"error": "任务队列已满，任务未被执行"})
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": "queue_full",
                                    "message": "任务队列已满，请稍后重试"}) from None

    log.info("任务已提交 task_id=%s customer=%s 附件=%d",
             task.task_id, payload.customer, len(dataset.files) if dataset else 0)
    return TaskResponse(
        task_id=task.task_id,
        status=task.status,
        created_at=task.created_at,
        message="任务已提交，正在处理中。可用 GET /api/v1/tasks/{task_id} 查询进度。",
    )


# ----------------------------------------------------------------------
# 2) 最近任务列表（必须声明在 /{task_id} 之前，见模块头注释）
# ----------------------------------------------------------------------
@router.get("/recent", response_model=TaskListResponse, summary="查询最近任务列表")
def list_recent_tasks(
    limit: Annotated[int, Query(ge=1, le=100, description="本页条数")] = 10,
    offset: Annotated[int, Query(ge=0, description="跳过的条数（分页用）")] = 0,
    db: Session = Depends(get_db),
) -> TaskListResponse:
    """分页返回最近任务，按创建时间倒序。"""
    items = crud.get_tasks_page(db, limit=limit, offset=offset)
    total = crud.count_tasks(db)
    return TaskListResponse(total=total, limit=limit, offset=offset,
                            items=[to_list_item(t) for t in items])


# ----------------------------------------------------------------------
# 3) 任务详情（含完整执行链路）
# ----------------------------------------------------------------------
@router.get("/{task_id}", response_model=TaskDetailResponse, summary="查询任务详情")
def get_task_detail(task_id: str, db: Session = Depends(get_db)) -> TaskDetailResponse:
    """查一个任务的详情 + 它执行过的每一次工具调用 / 闸门判定。"""
    task = crud.get_task_by_id(db, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "task_not_found",
                                    "message": f"任务 {task_id} 不存在"})
    executions = crud.get_tool_executions_by_task(db, task_id)
    return from_task(task, executions)


# ----------------------------------------------------------------------
# 4) 取消任务
# ----------------------------------------------------------------------
@router.post("/{task_id}/cancel", response_model=CancelResponse, summary="取消任务")
def cancel_task(task_id: str,
                db: Session = Depends(get_db),
                worker: TaskWorker = Depends(get_worker)) -> CancelResponse:
    """请求取消一个还没跑完的任务。

    大白话：取消是"协作式"的 —— 已经跑起来的任务没法从外部硬掐死，
    Agent 会在下一个检查点（每轮调用大模型之前）自己停下来。
    """
    task = crud.get_task_by_id(db, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "task_not_found",
                                    "message": f"任务 {task_id} 不存在"})

    if task.status in _TERMINAL_STATUSES:
        # 409 Conflict：语义上就是"当前状态不允许这个操作"，比 400 更准确
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "task_not_cancellable",
                    "message": f"任务已处于终态（{task.status}），无法取消"},
        )

    when = worker.request_cancel(task_id)
    if when == "running":
        # 正在执行：标志位已置起，但当前这一步（LLM 调用/工具执行）无法中断
        return CancelResponse(
            task_id=task_id, status="cancelled",
            message="取消指令已下达：将在当前步骤结束后生效（协作式取消）",
        )

    # 排队中，或状态是 processing 但已不在本 Worker 队列里（例如服务重启前遗留的任务）
    note = ("任务已取消" if when == "queued"
            else "任务不在当前队列中（可能由上次服务实例遗留），已标记为取消")
    crud.update_task_status(db, task_id, "cancelled", result_summary={"error": note})
    log.info("任务已取消 task_id=%s (when=%s)", task_id, when)
    return CancelResponse(task_id=task_id, status="cancelled", message=note)
```

---

## 13. `src/agent.py` 的改造点（全部标注 `# PHASE 2`）

三处改动，全部是**新增可选能力**，不传新参数时行为与 Phase 1 完全一致：

**① 新增取消信号异常**

```python
# PHASE 2: 任务取消信号
# 大白话：Web 层（API）需要支持"用户点取消"。Python 没法从外部硬杀死一个正在
#         跑的函数，所以改成"协作式取消"——外部把取消标志位置起来（threading.Event），
#         Agent 在执行到安全断点（每一轮 LLM 调用之前）时主动检查并停下来。
# 技术细节：为什么用异常而不是返回值？取消会从 _run_core 的内部循环里冒出来，
#         用异常可以直接穿透到 run() 统一处理，不必在每层都加 if 判断。
# ----------------------------------------------------------------------
class TaskCancelled(Exception):
    """任务被主动取消（协作式）。仅在传入 cancel_check 且返回 True 时抛出。"""
```

**② `run()`：支持复用外部（Web 层）已建好的 task_id，并处理取消终态**

> 为什么必须改：Web 层的 submit 接口已经建了一条 status=processing 的任务记录并立刻返回了
> `task_id`。如果 Agent 再自己 `_start_task()` 建一条，库里就会出现两条任务 —— 一条是接口返回给
> 用户的、一条是真干活的，审计直接错乱。

```python
    def run(self, user_input: str, customer: str = "访客",
            session_id: Optional[str] = None,
            task_id: Optional[str] = None,
            cancel_check: Optional[Callable[[], bool]] = None) -> dict:
        """处理一条工单 / 用户诉求，返回结构化结果（并留痕落库）。

        大白话：这是对外的"处理一条诉求"入口。它先开一张任务小票、把编号报给用户，
                再把真正的活交给 _run_core；无论成功还是炸了，都会回写任务状态 ——
                成功记 completed，炸了记 failed 并留下错误原因。

        session_id: 会话隔离键。不传时按客户分桶（cust:<客户名>），
                    保证不同客户之间不会共享对话上下文。

        task_id: PHASE 2 新增。任务编号。
                 - 不传（命令行/原有调用）：本方法自己建一条任务记录（PHASE 1 行为）。
                 - 传入（Web 层调用）：复用外部已经建好的那条任务记录，
                   避免"API 建一条、Agent 又建一条"的双份台账。
        cancel_check: PHASE 2 新增。协作式取消标志位检查函数，返回 True 表示
                 该任务已被要求取消。为 None 时永不取消（原有行为完全不变）。

        技术细节：主流程外面套 try/except 是为了保证"异常也必须落一条 failed"。
                  这里只做记录，异常仍原样抛出（不吞），既有行为不变。
                  TaskCancelled 是唯一被"吞掉"的异常 —— 取消不是故障，
                  它按正常终态（cancelled）落库并把结果返回给调用方。
        """
        # PHASE 1: 建任务台账（不可用时返回 None，后续所有落库动作自动跳过）
        # PHASE 2: 外部已给编号时不再重复建任务
        if task_id is None:
            task_id = self._start_task(user_input)
        try:
            result = self._run_core(user_input, customer, session_id, task_id,
                                    cancel_check)
        except TaskCancelled:
            # 取消是"预期内的终止"，不是故障：落 cancelled，返回结构化结果而不是抛异常
            cancelled_result: dict = {
                "answer": "任务已取消。", "cancelled": True, "task_id": task_id,
                "suggestions": [], "pending_approvals": [], "gate_outcomes": [],
                "trace": None, "session_id": session_id or f"cust:{customer}",
            }
            self._finish_task(task_id, "cancelled", result=cancelled_result,
                              error_message="任务被用户取消")
            log.info("任务被取消 task_id=%s", task_id)
            return cancelled_result
        except Exception as e:
            self._finish_task(task_id, "failed",
                              error_message=f"{type(e).__name__}: {e}")
            raise
        self._finish_task(task_id, "completed", result=result)
        return result
```

**③ `_run_core()`：新增 `cancel_check` 参数 + 两个取消检查点**

```python
    def _run_core(self, user_input: str, customer: str,
                  session_id: Optional[str], task_id: Optional[str],
                  cancel_check: Optional[Callable[[], bool]] = None) -> dict:
        """真正的主流程（即 PHASE 1 之前 run() 的原实现，业务逻辑保持不变）。"""
```

检查点 ①（每轮 LLM 调用前）：

```python
            # PHASE 2: 取消检查点 ①（每轮 LLM 调用之前）
            # 大白话：开始新一轮"思考"之前先看一眼有没有人按了取消，按了就立刻收工。
            # 技术细节：这是协作式取消粒度最细的位置 —— 一轮 = 一次 LLM 调用 + 一次
            #          工具执行。单个工具的耗时无法中断（不能硬杀线程），
            #          所以"取消"的最坏延迟 = 当前这一步的耗时。
            self._raise_if_cancelled(cancel_check)

            if self.mock:
```

检查点 ②（进建议生成之前）：

```python
        # PHASE 2: 取消检查点 ②（进建议生成之前）
        # 为什么单独放一个：mock 模式下一轮循环就 break 了，若只放在循环里，
        # mock 任务几乎没有可取消的窗口；这里是所有模式都会经过的必经之路。
        self._raise_if_cancelled(cancel_check)
        suggestions = self._build_suggestions(user_input, session_id=sid)
```

**④ 取消检查辅助方法**

```python
    def _raise_if_cancelled(cancel_check: Optional[Callable[[], bool]]) -> None:
        """到达取消检查点时判断是否已被要求取消。

        大白话：没传检查函数 = 这个任务不支持取消（命令行场景），直接放行。

        技术细节：cancel_check 由 Web 层传入，底层是 threading.Event.is_set()，
                 跨线程读是安全的（Event 自带内存屏障），所以在线程池里也能正确读到。
        """
        if cancel_check is not None and cancel_check():
            raise TaskCancelled("任务被用户取消")
```

**⑤ 回传思考链 `reasoning_content`（修复 Phase 1 遗留缺陷）**

这说明一下背景 —— 这是个**真实踩到的坑**，不改的话服务用真实模型跑起来会"看起来正常但其实是坏的"：

- `deepseek-v4-flash` 属于思考模式模型，返回 `tool_calls` 时会附带 `reasoning_content`；
- 下一轮把这个 assistant 消息发回去时，**必须原样带上该字段**，否则服务端直接判 400：
  `The reasoning_content in the thinking mode must be passed back to the API.`
- 现象是"第一轮工具调用成功、第二轮必失败"。而 `agent.py` 的主循环有 fail-safe
  （LLM 异常 → 降级为"升级人工"，不抛异常），于是**整件事看起来像模型自己不想干活**，
  极难定位。

配套的客户端改动（`src/models/client.py`）——把思维链暂存到实例属性，
**返回值仍是 `(content, tool_call)` 二元组**，不破坏既有调用方与测试桩：

```python
        # openai SDK 对非标准字段走 model_extra；两种取法都试一遍，兼容不同 SDK 版本
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning is None:
            extra = getattr(msg, "model_extra", None) or {}
            reasoning = extra.get("reasoning_content")
        self.last_reasoning_content = reasoning or None
```

agent 侧取用（用 `getattr` 兼容单测里的 LLM 桩）：

```python
            # 执行工具调用
            tool_result = self._dispatch_tool(tool_call, customer, task_id)
            assistant_msg: dict = {
                "role": "assistant",
                "content": content or "",
                "tool_calls": [{"id": tool_call["id"], "type": "function",
                                "function": {"name": tool_call["name"],
                                             "arguments": tool_call["arguments"]}}],
            }
            # PHASE 2 修复（Phase 1 遗留缺陷）：思考模式模型在返回 tool_calls 时会给出
            # 思维链 reasoning_content，**下一轮必须原样回传**，否则服务端判 400
            # （"The reasoning_content in the thinking mode must be passed back to the API"）。
            # 现象是"第一轮工具调用成功、第二轮必失败"，而 fail-safe 会把它降级成
            # "转人工"，看起来像模型自己不想干活，极难排查。
            # 用 getattr 取值：单测里的 LLM 桩没有这个属性，必须兼容。
            _reasoning = getattr(self.llm, "last_reasoning_content", None)
            if _reasoning:
                assistant_msg["reasoning_content"] = _reasoning
            messages.append(assistant_msg)
```

---

## 14. `src/persistence/crud.py` 的新增函数

Phase 1 的 `get_recent_tasks` 只会"取最近 N 条"，而 Web 接口要"第几页、每页几条 + 总数"。
**没有改造老函数**（它已被 `query_cli` 与测试使用），只做新增：

```python
def get_tasks_page(db: Session, limit: int = 10, offset: int = 0) -> list[Task]:
    """分页取任务列表，按创建时间倒序（同一秒再按自增 id 倒序，保证顺序稳定）。

    参数：
        limit:  本页条数（自动夹在 1..1000，防止调用方传负数或超大值）
        offset: 跳过前多少条（负数按 0 处理）

    技术细节：用 SQL 的 LIMIT/OFFSET 在库里分页，而不是把全部记录拉到内存再切片
    —— 数据量涨上来后内存占用是常数级。
    """
    try:
        n = min(1000, max(1, int(limit)))
    except (TypeError, ValueError):
        n = 10
    try:
        skip = max(0, int(offset))
    except (TypeError, ValueError):
        skip = 0
    stmt = (
        select(Task)
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(n)
        .offset(skip)
    )
    return list(db.execute(stmt).scalars().all())


def count_tasks(db: Session) -> int:
    """任务总数（配合 get_tasks_page，供前端计算总页数）。

    技术细节：用 `select(func.count())` 让数据库自己数，不把行拉回应用层。
    """
    return int(db.execute(select(func.count()).select_from(Task)).scalar_one())
```

---

## 15. 需求清单之外的 3 个模块

### 15.1 `src/api/settings.py`

```python
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
```

### 15.2 `src/api/dataset.py`

```python
# -*- coding: utf-8 -*-
"""上传文件 → 临时目录 → 任务级数据源（PHASE 2 新增）。

【这个文件是干什么的】
接口允许随任务一起上传 Excel（发货订单 / 退货订单 / 仓库退货订单）。这个模块负责
把"浏览器发过来的文件流"变成"Agent 真正能查到的数据"：

    接收文件流 → 落盘到系统临时目录 → 校验列名 → 统一转成 UTF-8 CSV
      → 组装成一个**只属于本次任务**的 DataSource → 任务跑完删掉临时目录

【为什么要单独做"任务级数据源"】
Phase 1 的数据源是模块级单例（`src.data_source.data_source`），全局只有一份。
如果直接把上传文件塞进这个单例，A 客户上传的订单会串到 B 客户的任务里 —— 数据串台。
所以这里用「临时替换 + 用完还原」的方式：在任务执行的这段窗口内，
把全局单例换成一份指向本任务临时文件的新实例，`finally` 里必定还原。

【安全考虑】
- 文件名只取最后一段（`Path(name).name`），杜绝 `../../etc/passwd` 这类路径穿越；
- 边读边计数，超过 `MAX_UPLOAD_SIZE` 立刻中断并删除半截文件，防止磁盘被写满；
- 只接受白名单扩展名，其余在落盘**之前**就拒绝；
- 临时文件处理完即删（除非显式开了 `KEEP_UPLOAD_FILES`）。
"""
from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from fastapi import UploadFile

from src.api.settings import api_settings
from src.logger import log

# 读取文件流时的分块大小（1MB）。边读边写边计数，避免把整个文件先吞进内存。
_CHUNK_SIZE: int = 1024 * 1024

# 订单 Excel 的必需列。提前校验是为了给出"缺哪一列"的明确报错，
# 而不是让数据源层在深层抛一个看不出所以然的 KeyError。
_ORDERS_REQUIRED_COLUMNS: set[str] = {
    "订单号", "商品编码", "商品名称", "规格", "数量", "单价", "门店",
}


# ----------------------------------------------------------------------
# 异常
# ----------------------------------------------------------------------
class UploadRejected(Exception):
    """上传被拒绝（文件太大 / 类型不支持 / 内容格式不对）。

    带 status_code 是为了让路由层能直接把它翻译成合适的 HTTP 状态码：
    400 = 请求有问题（类型/格式），413 = 体积超限。
    """

    def __init__(self, message: str, *, code: str = "invalid_upload", status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------
@dataclass
class UploadedFileInfo:
    """一个上传文件的落地信息（会写进 task 的流水里做留痕）。"""

    field: str                 # 表单字段名：delivery / return / warehouse
    filename: str              # 用户上传时的原始文件名
    size: int                  # 字节数
    saved_path: str            # 临时目录里的原始文件路径
    role: str                  # orders（驱动查询）/ returns（退货数据）/ archive（仅留痕）
    csv_path: str | None = None  # 转成 CSV 后的路径（仅 orders/returns 有）

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "filename": self.filename,
            "size": self.size,
            "role": self.role,
            "csv": bool(self.csv_path),
        }


@dataclass
class TaskDataset:
    """一次任务专属的上传数据集。"""

    temp_dir: Path
    orders_csv: Path | None = None     # 对应数据源的 orders_file
    returns_csv: Path | None = None    # 对应数据源的 returns_file
    files: list[UploadedFileInfo] = field(default_factory=list)

    @property
    def drives_query(self) -> bool:
        """本次上传的数据是否可以真正驱动查询。

        只有 orders（订单）能接入数据源 —— 退货文件在 Phase 1 的数据源里
        还没有对应的查询入口，上传它只做留痕。
        """
        return self.orders_csv is not None

    def summary(self) -> dict:
        """给审计/日志用的摘要（不包含临时绝对路径的完整信息，只留文件名与大小）。"""
        return {
            "temp_dir": str(self.temp_dir),
            "drives_query": self.drives_query,
            "files": [f.to_dict() for f in self.files],
        }


# ----------------------------------------------------------------------
# 表单字段 -> 数据角色 的映射
# ----------------------------------------------------------------------
# 需求给的三个上传字段是"业务口径"的名字，数据源要的是"文件口径"的名字，这里做对齐：
#   delivery  发货订单   -> orders   ：真正驱动 query_order / 建议生成
#   return    退货订单   -> returns  ：落到 returns_file，当前查询层暂未消费
#   warehouse 仓库退货单 -> archive  ：数据源没有对应文件，只做接收与留痕
_FIELD_ROLE: dict[str, str] = {
    "delivery": "orders",
    "return": "returns",
    "warehouse": "archive",
}


# ----------------------------------------------------------------------
# 落盘 + 转换
# ----------------------------------------------------------------------
async def _spool_to_disk(upload: UploadFile, dest: Path) -> int:
    """把上传流分块写入目标文件，并做体积守门。返回实际字节数。

    大白话：一边收一边数，超过上限就当场拒绝，不等到写完才发现太大。

    技术细节：`await upload.read(n)` 拿到的是 bytes 块；累计超过
    `MAX_UPLOAD_SIZE` 时抛 UploadRejected(413)，由调用方清理半截文件。
    """
    limit = api_settings.max_upload_size
    size = 0
    with dest.open("wb") as fh:
        while True:
            chunk = await upload.read(_CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise UploadRejected(
                    f"文件 {upload.filename} 超过大小上限 {limit} 字节",
                    code="payload_too_large",
                    status_code=413,
                )
            fh.write(chunk)
    if size == 0:
        raise UploadRejected(f"文件 {upload.filename} 是空文件", code="empty_upload")
    return size


def _to_utf8_csv(src: Path, dest: Path, role: str) -> None:
    """把上传文件统一转成数据源能读的 UTF-8 CSV。

    大白话：数据源只会读 CSV，而用户上传的多半是 Excel；这里做一次"翻译"。

    技术细节：
    - 用 pandas 读：Excel 走 read_excel（openpyxl 引擎），CSV 走 read_csv；
    - 写出时用 `encoding="utf-8-sig"`（带 BOM），与 Phase 1 数据源的读取口径一致，
      避免中文列名在 Windows 下被按 GBK 解码成乱码；
    - 订单文件额外做"必需列"体检，缺列直接给用户一句人话，而不是抛 KeyError。
    """
    suffix = src.suffix.lower()
    try:
        if suffix in (".xlsx", ".xls"):
            # .xls（老版二进制格式）需要 xlrd；没装时给出可执行的提示
            engine = "xlrd" if suffix == ".xls" else None
            df = pd.read_excel(src, engine=engine)
        else:
            df = pd.read_csv(src, encoding="utf-8-sig")
    except ImportError as e:  # pragma: no cover - 取决于环境是否装了 xlrd
        raise UploadRejected(
            f"读取 {src.suffix} 需要额外依赖：{e}", code="missing_reader"
        ) from e
    except Exception as e:
        raise UploadRejected(
            f"文件 {src.name} 无法解析（请确认是标准的 Excel/CSV 导出）：{e}",
            code="unparsable_file",
        ) from e

    if df.empty:
        raise UploadRejected(f"文件 {src.name} 没有数据行", code="empty_upload")

    if role == "orders":
        missing = _ORDERS_REQUIRED_COLUMNS - set(map(str, df.columns))
        if missing:
            raise UploadRejected(
                "订单文件缺少必需列："
                + "、".join(sorted(missing))
                + "。请使用极客云导出的订单明细（列名需与导出模板一致）",
                code="schema_mismatch",
            )

    df.to_csv(dest, index=False, encoding="utf-8-sig")


async def save_uploads(
    delivery: UploadFile | None = None,
    returns: UploadFile | None = None,
    warehouse: UploadFile | None = None,
) -> TaskDataset | None:
    """把三个可选上传文件落盘并转换成任务数据集。

    返回 None 表示这次请求没有带任何文件（走默认数据源）。

    异常：
        UploadRejected —— 类型不支持 / 体积超限 / 内容格式不对。
        任何失败都会把本次已写入的临时目录删干净，不留垃圾。
    """
    candidates: list[tuple[str, UploadFile]] = [
        (name, up)
        for name, up in (("delivery", delivery), ("return", returns), ("warehouse", warehouse))
        if up is not None and (up.filename or "").strip()
    ]
    if not candidates:
        return None

    temp_dir = Path(tempfile.mkdtemp(prefix="task-", dir=str(api_settings.upload_dir)))
    ds = TaskDataset(temp_dir=temp_dir)
    try:
        for field_name, upload in candidates:
            role = _FIELD_ROLE[field_name]
            safe_name = Path(upload.filename or "upload").name  # 只取文件名，杜绝路径穿越
            suffix = Path(safe_name).suffix.lower()
            if suffix not in api_settings.allowed_suffix_set:
                raise UploadRejected(
                    f"不支持的文件类型 {suffix or '(无扩展名)'}，"
                    f"允许：{api_settings.allowed_upload_suffixes}",
                    code="unsupported_type",
                )

            saved = temp_dir / safe_name
            size = await _spool_to_disk(upload, saved)

            csv_path: Path | None = None
            if role in ("orders", "returns"):
                csv_path = temp_dir / f"{role}.csv"
                _to_utf8_csv(saved, csv_path, role)

            ds.files.append(UploadedFileInfo(
                field=field_name, filename=safe_name, size=size,
                saved_path=str(saved), role=role,
                csv_path=str(csv_path) if csv_path else None,
            ))
            if role == "orders":
                ds.orders_csv = csv_path
            elif role == "returns":
                ds.returns_csv = csv_path

        log.info("上传文件已就绪: %s", ds.summary())
        return ds
    except Exception:
        # 出错就把这次任务目录整个删掉：半截文件比没有文件更危险
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def cleanup(ds: TaskDataset | None) -> None:
    """任务处理完后清理临时目录（幂等，重复调用安全）。"""
    if ds is None or api_settings.keep_upload_files:
        return
    shutil.rmtree(ds.temp_dir, ignore_errors=True)


# ----------------------------------------------------------------------
# 任务级数据源：临时替换全局单例，用完必定还原
# ----------------------------------------------------------------------
@contextmanager
def install_dataset(ds: TaskDataset | None) -> Iterator[None]:
    """在 with 作用域内，把全局数据源换成"本次任务上传的文件"。

    大白话：这段时间里 Agent 查的就是你要的那份表；出了这个 with，
    一切照旧（还是项目自带的 data/mock 数据）。

    技术细节：需要打两个补丁，少一个都会出现"有的工具读到新数据、有的读到旧数据"：
    - `src.data_source.data_source`：agent.py / safety/gate.py / llm_suggestion.py
      都是在函数里 `from src.data_source import data_source`（调用时才取），
      所以改模块属性即可生效；
    - `src.tools.business_tools.data_source`：它在模块顶层就 `from ... import data_source`
      把名字绑进了自己的命名空间，必须单独再改一次。

    并发注意：这是对全局状态的临时改写，所以**必须**保证同一时刻只有一个任务在跑。
    这个约束由 `worker.py` 里的执行锁负责，不要把本函数用到别处。
    """
    if ds is None or not ds.drives_query or not api_settings.use_uploaded_dataset:
        # 没上传、上传的不驱动查询、或显式关闭了该能力 -> 保持默认数据源
        yield
        return

    import src.data_source as data_source_module
    import src.tools.business_tools as business_tools_module
    from src.data_source import DataSource

    overrides: dict[str, str] = {}
    if ds.orders_csv is not None:
        overrides["orders_file"] = str(ds.orders_csv)
    if ds.returns_csv is not None:
        overrides["returns_file"] = str(ds.returns_csv)

    task_source = DataSource(**overrides)
    try:
        # 提前 load：列名不对/文件损坏会在这一步暴露，报错信息比运行时随机崩更清楚
        task_source.load()
    except Exception as e:
        raise UploadRejected(
            f"上传的数据文件无法加载（请确认列名与极客云导出一致）：{e}",
            code="dataset_load_failed",
        ) from e

    old_module_source = data_source_module.data_source
    old_tools_source = business_tools_module.data_source
    data_source_module.data_source = task_source
    business_tools_module.data_source = task_source
    log.info("本次任务启用上传数据集: orders=%s returns=%s",
             ds.orders_csv, ds.returns_csv)
    try:
        yield
    finally:
        # 无论任务成功、失败还是被取消，都必须还原，否则会污染下一个任务
        data_source_module.data_source = old_module_source
        business_tools_module.data_source = old_tools_source
```

### 15.3 `src/api/worker.py`

```python
# -*- coding: utf-8 -*-
"""异步任务队列 + 后台 Worker（PHASE 2 新增）。

【为什么需要它】
`Agent.run()` 是**同步阻塞**的：它要调大模型、读数据文件，一次可能跑几秒到几十秒。
如果在 HTTP 请求里直接调用，事件循环会被卡住 —— 期间所有其他请求（包括健康检查）
全部排队等它跑完。所以架构改成"接单即返回，后台慢慢做"：

    POST /submit ──> 建任务记录(processing) ──> 丢进 asyncio.Queue ──> 立刻返回 task_id
                                                      │
                            后台 Worker 协程 <─────────┘
                                    │
                                    └─ asyncio.to_thread(agent.run)  ← 丢到线程里跑，不阻塞事件循环
                                            │
                                            └─ 成功/失败/取消 → 回写任务终态

【为什么用 asyncio.to_thread 而不是把 Agent 改成 async】
Phase 1 的 Agent 与 4 个业务工具都是同步实现（openai SDK 同步客户端、pandas 读文件）。
改 async 相当于重写整条链路，风险远大于收益。`to_thread` 是标准做法：
让同步函数在独立线程里跑，事件循环照常服务其他请求。

【为什么执行阶段要加锁串行】
两个原因，都不是"为了保险"这么含糊：
1. `dataset.install_dataset` 会**临时改写全局数据源单例**，两个任务同时跑会串数据；
2. Agent 内部持有会话记忆 / 安全闸门 / 追踪器等进程内单例。
所以 `WORKERS>1` 目前只在"取任务"这一层并行，真正执行仍是串行的。
要真正并行，需要把 Agent 改成无状态（每任务独立实例 + 独立数据源），
这是后续阶段的事 —— 这里把约束显式写出来，而不是假装支持。
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

from src.agent import TaskCancelled, TicketAgent, create_agent
from src.api.dataset import TaskDataset
from src.api.dataset import cleanup as cleanup_dataset
from src.api.dataset import install_dataset
from src.api.settings import api_settings
from src.logger import log
from src.persistence import crud
from src.persistence.database import get_db as _session_scope


# ----------------------------------------------------------------------
# 队列元素
# ----------------------------------------------------------------------
@dataclass
class TaskJob:
    """一条待处理任务（队列里流动的东西）。"""

    task_id: str                 # 已在数据库里建好的任务编号
    user_input: str              # 用户的自然语言诉求
    customer: str                # 客户名（同时作为会话隔离键）
    session_id: Optional[str] = None   # 显式会话键；不传则由 Agent 按客户分桶
    dataset: Optional[TaskDataset] = None   # 本次上传的数据文件（可空）


# ----------------------------------------------------------------------
# 任务终态回写（集中一处，避免散落）
# ----------------------------------------------------------------------
def _finalize(task_id: str, status: str, *, summary: Optional[dict] = None,
              only_if_open: bool = False) -> None:
    """把任务状态写回数据库。

    大白话：把"这件事最后怎么样了"记到账本上。

    技术细节：写库失败只记 ERROR 日志、绝不向上抛 —— 账本写不进去不该让 Worker 挂掉。
    `only_if_open=True` 表示"仅当任务还没结束才覆盖"，避免把已经 completed
    的任务误改成 failed（例如异常发生在收尾阶段时）。
    """
    try:
        with _session_scope() as db:
            if only_if_open:
                task = crud.get_task_by_id(db, task_id)
                if task is None or task.status in ("completed", "failed", "cancelled"):
                    return
            crud.update_task_status(db, task_id, status, result_summary=summary)
    except Exception as e:  # pragma: no cover - 只有库挂了才会走到
        log.error("Worker：回写任务状态失败(task_id=%s -> %s): %s", task_id, status, e)


# ----------------------------------------------------------------------
# 默认 Agent 工厂
# ----------------------------------------------------------------------
def build_default_agent() -> TicketAgent:
    """按配置造出跑任务用的 Agent 实例。

    大白话：`AGENT_FORCE_MOCK=1` 时完全不碰大模型（离线演示、自动化测试用）；
    否则有 key 就走真实模型、没 key 自动降级 mock（Phase 1 既有行为）。
    """
    agent = create_agent(mock=api_settings.agent_force_mock)
    log.info("Worker：Agent 已就绪 mock=%s use_llm=%s persistence=%s",
             agent.mock, agent.use_llm, agent.persist)
    return agent


# ----------------------------------------------------------------------
# Worker
# ----------------------------------------------------------------------
class TaskWorker:
    """后台任务消费者。

    生命周期由 FastAPI 的 lifespan 管理：启动时 `await start()`，关闭时 `await stop()`。
    """

    def __init__(self,
                 agent_factory: Optional[Callable[[], TicketAgent]] = None,
                 concurrency: Optional[int] = None) -> None:
        self._agent_factory = agent_factory or build_default_agent
        self._concurrency = max(1, int(concurrency or api_settings.workers))
        self._queue: asyncio.Queue[TaskJob] = asyncio.Queue(
            maxsize=max(0, int(api_settings.max_queue_size))
        )
        self._consumers: list[asyncio.Task] = []
        self._agent: Optional[TicketAgent] = None

        # 同一时刻只允许一个任务真正执行（原因见模块头注释）
        self._run_lock = threading.Lock()

        # 取消标志位：task_id -> threading.Event。
        # 用 Event 而不是 bool，是因为它要被"事件循环线程"写、"工作线程"读，
        # Event 内部有锁保证跨线程可见性；普通变量在 CPython 虽然大概率也能读到，
        # 但没有内存可见性保证，属于"能跑但不对"。
        self._cancel_flags: dict[str, threading.Event] = {}
        self._queued: set[str] = set()      # 已入队、尚未开始执行
        self._running: set[str] = set()     # 正在执行
        self._started = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动 Worker 协程。重复调用安全（幂等）。"""
        if self._started:
            return
        # 队列每次启动重建：asyncio 的 Queue 绑定"第一次使用它的那个事件循环"，
        # 跨事件循环复用会直接抛 RuntimeError（测试里反复建/停服务就会踩到）。
        # 反正启动时队列本来就该是空的，重建没有任何副作用。
        self._queue = asyncio.Queue(maxsize=max(0, int(api_settings.max_queue_size)))
        self._agent = self._agent_factory()
        self._consumers = [
            asyncio.create_task(self._consume(i), name=f"agent-worker-{i}")
            for i in range(self._concurrency)
        ]
        self._started = True
        log.info("后台 Worker 已启动 (workers=%d, queue_max=%s)",
                 self._concurrency, api_settings.max_queue_size or "无上限")

    async def stop(self) -> None:
        """停止 Worker（取消协程并等待退出）。"""
        self._started = False
        for task in self._consumers:
            task.cancel()
        if self._consumers:
            await asyncio.gather(*self._consumers, return_exceptions=True)
        self._consumers = []
        log.info("后台 Worker 已停止")

    # ------------------------------------------------------------------
    # 生产端
    # ------------------------------------------------------------------
    async def submit(self, job: TaskJob) -> None:
        """把任务放进队列。

        异常：
            asyncio.QueueFull —— 队列满了。调用方（路由）应翻译成 503，
            提醒调用方稍后重试，而不是无限期把请求堆在内存里。
        """
        await self._queue.put(job)
        self._queued.add(job.task_id)
        self._cancel_flags.setdefault(job.task_id, threading.Event())
        log.info("任务已入队 task_id=%s (队列长度=%d)", job.task_id, self._queue.qsize())

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------
    def request_cancel(self, task_id: str) -> str:
        """请求取消任务。返回取消生效的时机：queued / running / unknown。

        大白话：取消是"协作式"的 —— 我们只能立个牌子告诉 Agent"别干了"，
                没法从外面直接把正在跑的函数掐死。所以：
                - 还在排队 → 立刻取消，Worker 取到它时会跳过；
                - 正在执行 → 设置标志位，Agent 跑到下一个检查点（每轮 LLM 调用前）就停。
        """
        flag = self._cancel_flags.get(task_id)
        if task_id in self._running:
            if flag is not None:
                flag.set()
            return "running"
        if task_id in self._queued:
            if flag is not None:
                flag.set()
            return "queued"
        return "unknown"

    # ------------------------------------------------------------------
    # 消费端
    # ------------------------------------------------------------------
    async def _consume(self, index: int) -> None:
        """消费者协程：不停地从队列取任务并处理。"""
        while True:
            job = await self._queue.get()
            try:
                await self._handle(job)
            except asyncio.CancelledError:
                # 关闭服务时被取消：必须落一个**终态**，否则这条任务会永远停在
                # processing，调用方轮询到天荒地老也等不到结果。
                _finalize(job.task_id, "failed",
                          summary={"error": "服务关闭导致任务中断，请重新提交"},
                          only_if_open=True)
                cleanup_dataset(job.dataset)
                raise
            except Exception as e:  # pragma: no cover - 兜底，保证单个任务炸不掉整个 Worker
                log.exception("Worker#%d 处理任务异常 task_id=%s: %s", index, job.task_id, e)
                _finalize(job.task_id, "failed",
                          summary={"error": f"{type(e).__name__}: {e}"},
                          only_if_open=True)
            finally:
                self._queue.task_done()

    async def _handle(self, job: TaskJob) -> None:
        """处理单个任务：执行 + 清理。"""
        flag = self._cancel_flags.setdefault(job.task_id, threading.Event())
        self._queued.discard(job.task_id)

        # 排队期间就被取消 -> 连跑都不用跑
        if flag.is_set():
            log.info("任务在排队期间被取消，跳过执行 task_id=%s", job.task_id)
            _finalize(job.task_id, "cancelled",
                      summary={"error": "任务在排队期间被取消"}, only_if_open=True)
            self._cancel_flags.pop(job.task_id, None)
            cleanup_dataset(job.dataset)
            return

        self._running.add(job.task_id)
        try:
            # 关键：同步的 Agent.run 丢进线程池，事件循环立刻空出来继续服务 HTTP 请求
            await asyncio.to_thread(self._execute, job, flag)
        finally:
            self._running.discard(job.task_id)
            self._cancel_flags.pop(job.task_id, None)
            cleanup_dataset(job.dataset)   # 上传的临时文件用完即删

    def _execute(self, job: TaskJob, flag: threading.Event) -> None:
        """在工作线程里真正执行 Agent（同步阻塞）。"""
        with self._run_lock:
            try:
                # 用本次任务上传的数据源（没上传就是默认的 data/mock）
                with install_dataset(job.dataset):
                    result = self._require_agent().run(
                        job.user_input,
                        customer=job.customer,
                        session_id=job.session_id,
                        task_id=job.task_id,          # 复用 API 已建好的任务编号
                        cancel_check=flag.is_set,     # 协作式取消：Agent 到检查点自查
                    )
                log.info("任务处理完成 task_id=%s status=%s",
                         job.task_id, "cancelled" if result.get("cancelled") else "completed")
            except TaskCancelled:
                # 真实 Agent 会在 run() 内部就把状态落成 cancelled（且摘要更完整），
                # 走到这里时 only_if_open 会让它自动跳过。
                # 保留这一次兜底是为了保证"终态由 Worker 负责"这个不变量：
                # 只要有任何一条路径抛出了 TaskCancelled 而没落库，
                # 任务也绝不会永远停在 processing 上。
                _finalize(job.task_id, "cancelled",
                          summary={"error": "任务被用户取消"}, only_if_open=True)
                log.info("任务已取消 task_id=%s", job.task_id)
            except Exception as e:
                # run() 已经落过 failed；这里再兜一次是防止异常发生在 run() 之外
                # （比如 install_dataset 阶段数据文件加载失败）
                log.exception("任务执行失败 task_id=%s", job.task_id)
                _finalize(job.task_id, "failed",
                          summary={"error": f"{type(e).__name__}: {e}"},
                          only_if_open=True)

    # ------------------------------------------------------------------
    def _require_agent(self) -> TicketAgent:
        if self._agent is None:  # pragma: no cover - start() 之前不该被调用
            self._agent = self._agent_factory()
        return self._agent

    @property
    def stats(self) -> dict:
        """运行指标（/metrics 用）。"""
        return {
            "queue_size": self._queue.qsize(),
            "running": len(self._running),
            "workers": self._concurrency,
            "worker_alive": self._started and any(not t.done() for t in self._consumers),
        }


# 全局唯一 Worker（与 Phase 1 的 settings / tracer 同风格：模块级单例 + 可注入）
task_worker = TaskWorker()
```

---

## 16. 测试

### 16.1 `tests/conftest.py`（隔离配置）

新增 `AGENT_FORCE_MOCK=1`。**为什么必须加**：项目 `.env` 里通常配了真实模型 key，
不拦一下，API 测试每提交一个任务就真的去调一次大模型 —— 慢、花钱、依赖网络、
结果不可复现。

```python
# -*- coding: utf-8 -*-
"""pytest 全局配置。

作用有两个：**把测试期的持久化数据库重定向到临时目录**、**强制 Agent 走 mock 模式**。

为什么要重定向数据库：Phase 1 之后 Agent 每跑一次就写一次 SQLite（默认
data/agent_operations.db）。如果不管，`pytest` 一跑就会把测试数据灌进真实审计库，
历史记录里混进一堆 "测试客户" 的任务 —— 审计库最忌讳这个。

为什么要强制 mock（AGENT_FORCE_MOCK）：项目的 .env 里通常配了真实的模型 key，
不拦一下，Phase 2 的 API 测试每提交一个任务就会真的去调一次大模型 ——
既慢、又要花钱、还依赖网络，测试结果不可复现。

做法：在**收集测试模块之前**设置这两个环境变量。
- AGENT_DB_PATH   由 src/persistence/database.py 在导入时读取；
- AGENT_FORCE_MOCK 由 src/api/settings.py 读取（api_settings.agent_force_mock）。
测试自己的用例（含 test_persistence.py / test_api.py）会进一步用临时引擎
把自己隔离到各自独立的小库，互不干扰。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="agent-test-db-"))
# setdefault 而不是直接赋值：允许外部显式指定时以外部为准
os.environ.setdefault("AGENT_DB_PATH", str(_TEST_DB_DIR / "agent_operations.db"))
# PHASE 2：测试一律不调真实大模型（有 .env key 也不会被用上）
os.environ.setdefault("AGENT_FORCE_MOCK", "1")
```

### 16.2 `tests/test_api.py`（25 项）

```python
# -*- coding: utf-8 -*-
"""Phase 2 Web 服务层测试。

【这份测试在验什么】
不是"接口能返回 200"这种表面验收，而是逐条验**架构承诺**：
1. 提交是异步的：submit 立刻返回 processing，结果是后台 Worker 写回来的；
2. 上传文件**真的**驱动了查询（用一份本地数据源里绝对没有的订单来判定）；
3. 任务级数据源用完全还原，不会串到下一个任务；
4. 上传的临时文件处理完被清掉；
5. 取消是协作式的，能让正在跑的任务停下来并落 cancelled；
6. 各类错误都是统一 JSON 形状 + 合适的 HTTP 状态码。

【为什么整个文件共用一个 client】
TestClient 每次进 with 都会新建一个事件循环，而后台 Worker 是模块级单例。
共用 client 既贴近"服务一直开着"的真实情况，也避免反复启停带来的干扰。
"""
from __future__ import annotations

import io
import json
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.api.settings import api_settings

# ----------------------------------------------------------------------
# 测试用的订单 Excel：列名与极客云导出模板完全一致
# （data_source 读的就是这套列名，列名对不上会被明确拒绝）
# ----------------------------------------------------------------------
_ORDER_COLUMNS = [
    "订单号", "订单创建时间", "付款时间", "商品编码", "商品名称", "规格", "数量",
    "单价", "商品金额", "门店", "收货人", "联系电话", "收货地址", "订单状态",
    "渠道", "支付方式", "物流单号",
]

# 这个订单号在项目自带的 data/mock 数据源里**不存在**，
# 所以"能不能查到它"正好可以判定本次任务用的是默认数据源还是上传的数据源。
_GHOST_ORDER = "PO20260929-88888"


def _orders_xlsx(order_id: str = _GHOST_ORDER, qty: int = 3,
                 store: str = "苏州工业园店", sku: str = "XC-JLY008") -> bytes:
    """按极客云导出的列名，造一份只有一条明细的订单 Excel。"""
    row = [order_id, "2026-09-29 10:00:00", "2026-09-29 10:01:00", sku, "玻璃水", "2L",
           qty, "29.9", "89.7", store, "王五", "13800000000", "杭州市西湖区",
           "已发货", "门店", "微信", "SF123456789"]
    buf = io.BytesIO()
    pd.DataFrame([row], columns=_ORDER_COLUMNS).to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _submit(client: TestClient, text: str, customer: str = "测试客户", **files):
    data = {"user_input": text, "customer": customer}
    return client.post("/api/v1/tasks/submit", data=data, files=files or None)


def _wait(client: TestClient, task_id: str, timeout: float = 20.0) -> dict:
    """轮询直到任务离开 processing（异步处理，测试必须等）。"""
    deadline = time.time() + timeout
    detail = {}
    while time.time() < deadline:
        detail = client.get(f"/api/v1/tasks/{task_id}").json()
        if detail.get("status") != "processing":
            return detail
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 超时未结束，最后状态：{detail}")


def _gate_outcome(detail: dict) -> dict:
    """从任务摘要里取出安全闸门对第一个动作的判定。"""
    summary = json.loads(detail["result_summary"])
    return summary["gate_outcomes"][0]


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    """整个模块共用的 TestClient（进入 with 才会触发 lifespan：建表 + 起 Worker）。"""
    from src.api.main import app

    with TestClient(app) as c:
        yield c


# ----------------------------------------------------------------------
# 1) 系统接口
# ----------------------------------------------------------------------
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["version"] == api_settings.api_version


def test_root_hint(client):
    body = client.get("/").json()
    assert body["docs"] == "/docs"
    assert "submit" in body


def test_openapi_docs_available(client):
    """/docs 能打开的前提是 OpenAPI schema 能生成。"""
    assert client.get("/openapi.json").status_code == 200


def test_metrics(client):
    body = client.get("/metrics").json()
    assert body["workers"] >= 1
    assert body["worker_alive"] is True
    assert "total" in body["today"]


# ----------------------------------------------------------------------
# 2) 提交 + 异步处理
# ----------------------------------------------------------------------
def test_submit_returns_immediately_and_completes_in_background(client):
    r = _submit(client, "客户要求补发一瓶玻璃水", customer="异步验证")
    assert r.status_code == 200
    body = r.json()
    # 关键点：接口当场返回 processing，而不是等 Agent 跑完
    assert body["status"] == "processing"
    assert body["task_id"].startswith("task_")
    assert body["created_at"]

    detail = _wait(client, body["task_id"])
    assert detail["status"] == "completed"
    assert detail["user_input"] == "客户要求补发一瓶玻璃水"
    assert detail["updated_at"] >= detail["created_at"]


def test_task_detail_contains_execution_chain(client):
    """查详情要能看到"当时到底做了什么" —— 这是 Phase 1 审计能力在 Web 上的出口。"""
    r = _submit(client, "客户反映订单 PO20260928-00001 少发了一瓶玻璃水，要求补发",
                customer="链路验证")
    detail = _wait(client, r.json()["task_id"])
    tools = [e["tool_name"] for e in detail["executions"]]
    assert "safety_gate" in tools          # 敏感动作一定过闸门并留痕
    gate = next(e for e in detail["executions"] if e["tool_name"] == "safety_gate")
    assert gate["status"] in ("success", "blocked")
    assert isinstance(gate["input_params"], dict)   # 库里存的 JSON 字符串已还原成对象


def test_submit_missing_user_input_422(client):
    r = client.post("/api/v1/tasks/submit", data={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_submit_blank_user_input_422(client):
    r = client.post("/api/v1/tasks/submit", data={"user_input": ""})
    assert r.status_code == 422


def test_detail_not_found_404(client):
    r = client.get("/api/v1/tasks/task_does_not_exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "task_not_found"


# ----------------------------------------------------------------------
# 3) 列表分页
# ----------------------------------------------------------------------
def test_recent_list_shape(client):
    body = client.get("/api/v1/tasks/recent?limit=5&offset=0").json()
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert body["total"] >= 1
    assert len(body["items"]) <= 5
    assert set(body["items"][0]) == {"task_id", "user_input", "status", "created_at"}


def test_recent_pagination_does_not_overlap(client):
    first = client.get("/api/v1/tasks/recent?limit=2&offset=0").json()["items"]
    second = client.get("/api/v1/tasks/recent?limit=2&offset=2").json()["items"]
    assert {i["task_id"] for i in first}.isdisjoint({i["task_id"] for i in second})


def test_recent_route_not_shadowed_by_task_id(client):
    """/recent 不能被 /{task_id} 抢走（否则会被当成 task_id="recent" 查库）。"""
    assert client.get("/api/v1/tasks/recent").status_code == 200


def test_recent_limit_out_of_range_422(client):
    r = client.get("/api/v1/tasks/recent?limit=999")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


# ----------------------------------------------------------------------
# 4) 上传
# ----------------------------------------------------------------------
def test_upload_unsupported_suffix_rejected(client):
    r = client.post("/api/v1/tasks/submit", data={"user_input": "x"},
                    files={"delivery": ("a.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unsupported_type"


def test_upload_empty_file_rejected(client):
    r = client.post("/api/v1/tasks/submit", data={"user_input": "x"},
                    files={"delivery": ("a.csv", b"", "text/csv")})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "empty_upload"


def test_upload_wrong_columns_rejected(client):
    """列名不对要给出人话报错，而不是等数据源在深层抛 KeyError。"""
    buf = io.BytesIO()
    pd.DataFrame([{"商品": "x"}]).to_excel(buf, index=False, engine="openpyxl")
    r = client.post("/api/v1/tasks/submit", data={"user_input": "x"},
                    files={"delivery": ("b.xlsx", buf.getvalue(), _XLSX_MIME)})
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "schema_mismatch"
    assert "缺少必需列" in body["message"]


def test_upload_recorded_in_execution_chain(client):
    """附件要留痕：审计得能回答"这一单当时带了什么材料"。"""
    r = client.post(
        "/api/v1/tasks/submit",
        data={"user_input": "客户要求补发一瓶玻璃水", "customer": "附件留痕"},
        files={"delivery": ("orders.xlsx", _orders_xlsx(), _XLSX_MIME)},
    )
    assert r.status_code == 200
    detail = _wait(client, r.json()["task_id"])
    upload = next(e for e in detail["executions"] if e["tool_name"] == "file_upload")
    assert upload["status"] == "success"
    files = upload["input_params"]["files"]
    assert files[0]["field"] == "delivery"
    assert files[0]["role"] == "orders"
    assert upload["output_result"]["drives_query"] is True


def test_uploaded_dataset_actually_drives_the_query(client):
    """**核心用例**：上传的订单文件必须真的参与查询。

    判定方式：用一份"默认数据源里根本没有"的订单。
    - 不带附件跑一次：闸门会因为"订单不存在"拒绝补发；
    - 带上附件再跑：订单能定位到、SKU 能取出来，闸门放行进人工确认。
    两次结果不同，才能证明上传的数据真的被用上了（否则"返回 200"说明不了任何事）。
    """
    text = f"客户反映订单 {_GHOST_ORDER} 少发了一瓶玻璃水，要求补发"

    # A：不带附件 -> 默认数据源查不到这个订单
    without = _wait(client, _submit(client, text, customer="无附件").json()["task_id"])
    a = _gate_outcome(without)
    assert a["params"]["order_id"] == _GHOST_ORDER
    assert a["params"]["inventory_note"].startswith("订单") and "无法定位" in a["params"]["inventory_note"]

    # B：带附件 -> 上传的数据源里有这个订单
    r = client.post("/api/v1/tasks/submit",
                    data={"user_input": text, "customer": "有附件"},
                    files={"delivery": ("orders.xlsx", _orders_xlsx(), _XLSX_MIME)})
    with_upload = _wait(client, r.json()["task_id"])
    b = _gate_outcome(with_upload)
    assert b["params"]["order_id"] == _GHOST_ORDER
    assert b["params"].get("sku") == "XC-JLY008"      # 从上传的订单里取到了 SKU
    assert "无法定位" not in b["params"].get("inventory_note", "")


def test_dataset_is_restored_after_upload_task(client):
    """任务级数据源用完必须还原，否则上传的数据会串到下一个任务（数据串台）。"""
    # 先跑一个带附件的任务
    r = client.post("/api/v1/tasks/submit",
                    data={"user_input": f"客户反映订单 {_GHOST_ORDER} 少发了一瓶，要求补发"},
                    files={"delivery": ("orders.xlsx", _orders_xlsx(), _XLSX_MIME)})
    _wait(client, r.json()["task_id"])

    # 再跑一个不带附件的任务：应该回到默认数据源，查不到那个"幽灵订单"
    after = _wait(client, _submit(client, f"客户反映订单 {_GHOST_ORDER} 少发了一瓶，要求补发",
                                  customer="还原验证").json()["task_id"])
    outcome = _gate_outcome(after)
    assert "无法定位" in outcome["params"].get("inventory_note", ""), \
        "上一个任务的上传数据串到了下一个任务，数据源没有被还原"


def test_upload_temp_dir_cleaned(client):
    """上传的临时文件处理完要删掉，不给磁盘留垃圾。"""
    upload_root = api_settings.upload_dir
    before = {p.name for p in upload_root.iterdir() if p.is_dir()}
    r = client.post("/api/v1/tasks/submit",
                    data={"user_input": "客户要求补发一瓶玻璃水"},
                    files={"delivery": ("orders.xlsx", _orders_xlsx(), _XLSX_MIME)})
    _wait(client, r.json()["task_id"])
    after = {p.name for p in upload_root.iterdir() if p.is_dir()}
    assert not (after - before), f"任务结束后仍残留临时目录: {after - before}"


# ----------------------------------------------------------------------
# 5) 取消
# ----------------------------------------------------------------------
def test_cancel_nonexistent_404(client):
    assert client.post("/api/v1/tasks/task_nope/cancel").status_code == 404


def test_cancel_finished_task_409(client):
    r = _submit(client, "客户要求补发一瓶玻璃水", customer="终态取消")
    task_id = r.json()["task_id"]
    _wait(client, task_id)
    resp = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_not_cancellable"


def test_cancel_running_task(client, monkeypatch):
    """协作式取消：正在跑的任务要能被叫停并落 cancelled。

    做法：把 Worker 里的 Agent 临时换成一个"会一直等取消信号"的假 Agent。
    这样取消请求到达时任务一定还在处理中，测试不依赖运气/时序。

    假 Agent 也必须遵守**真 Agent 的契约**（自己回写任务终态），
    否则测的就不是取消逻辑，而是"假实现忘了落库"。
    """
    from src.agent import TaskCancelled
    from src.api.worker import _finalize, task_worker

    class BlockingAgent:
        """模拟跑很久的 Agent：每 50ms 检查一次取消标志。"""

        def run(self, user_input, customer="访客", session_id=None,
                task_id=None, cancel_check=None):
            for _ in range(200):                      # 最多 10 秒
                if cancel_check is not None and cancel_check():
                    raise TaskCancelled("测试：被取消")
                time.sleep(0.05)
            _finalize(task_id, "completed", summary={"answer": "本不该跑到这里"})
            return {"answer": "本不该跑到这里", "task_id": task_id}

    monkeypatch.setattr(task_worker, "_agent", BlockingAgent())

    r = _submit(client, "一个会跑很久的任务", customer="取消验证")
    task_id = r.json()["task_id"]

    # 等它真的开始执行（进入 running 状态）再取消
    for _ in range(100):
        if client.get("/metrics").json()["running"] == 1:
            break
        time.sleep(0.05)

    resp = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["task_id"] == task_id

    detail = _wait(client, task_id, timeout=15)
    assert detail["status"] == "cancelled"


def test_cancel_queued_task(client, monkeypatch):
    """排队期间就被取消的任务：连执行都不该执行。"""
    from src.api.worker import _finalize, task_worker

    class SlowAgent:
        def run(self, user_input, customer="访客", session_id=None,
                task_id=None, cancel_check=None):
            time.sleep(1.0)
            _finalize(task_id, "completed", summary={"answer": "done"})
            return {"answer": "done", "task_id": task_id}

    # 先占住 Worker：塞一个慢任务，后面提交的任务只能排队
    monkeypatch.setattr(task_worker, "_agent", SlowAgent())
    occupied = _submit(client, "占位任务", customer="占位").json()["task_id"]

    queued = _submit(client, "排队中的任务", customer="排队验证").json()["task_id"]
    assert client.post(f"/api/v1/tasks/{queued}/cancel").json()["status"] == "cancelled"

    detail = _wait(client, queued, timeout=15)
    assert detail["status"] == "cancelled"
    # 摘要里要能看出"它压根没跑"，而不是"跑到一半被停"
    assert "取消" in (detail["result_summary"] or "")

    _wait(client, occupied, timeout=15)


# ----------------------------------------------------------------------
# 6) 向后兼容：CLI 入口还能用
# ----------------------------------------------------------------------
def test_agent_run_signature_still_backward_compatible():
    """Phase 1 的调用方式（只传 user_input/customer）必须继续可用。"""
    import inspect

    from src.agent import TicketAgent

    params = inspect.signature(TicketAgent.run).parameters
    assert list(params)[:3] == ["self", "user_input", "customer"]
    for name in ("task_id", "cancel_check"):
        assert params[name].default is None      # 新增参数必须都是可选的
```

---

## 17. 验收结果（全部实测）

| 验收项 | 结果 |
|---|---|
| `uvicorn src.api.main:app` 能正常启动 | ✅ 实测启动 1 秒内就绪 |
| `/docs` 能打开自动生成的 API 文档 | ✅ HTTP 200（`/openapi.json` 200） |
| `/health` 返回 healthy | ✅ `{"status":"healthy","version":"1.0.0"}` |
| `POST /api/v1/tasks/submit` 返回 task_id | ✅ `{"task_id":"task_aa1be0ca78c44b16","status":"processing",...}` |
| `GET /api/v1/tasks/{task_id}` 能查到详情与工具执行记录 | ✅ 含 `safety_gate` 闸门判定与真实工具流水 |
| `GET /api/v1/tasks/recent` 能查最近任务 | ✅ `total/limit/offset/items` |
| 任务后台异步处理，不阻塞 HTTP 响应 | ✅ **submit 接口耗时 95ms**，随即返回 processing；约 2 秒后状态变为 completed |
| 原有 71 项 + 17 项 + 新增 25 项测试全部通过 | ✅ `113 passed in 25.69s`（88 原有 + 25 新增） |
| `docker-compose up` 一键启动 | ⚠️ 见下方说明 |
| `python src/agent.py` 命令行入口仍可用 | ✅ 实测仍能启动并给出建议 |
| `scripts/run.py`、`python src/query_cli.py` 仍可用 | ✅ 实测正常 |
| 真实大模型链路可用 | ✅ 见下 |

**关于 docker-compose**：当前环境未安装 Docker（`docker: command not found`），
我**无法实际构建镜像验证**。Dockerfile / compose 已按最佳实践编写并做了针对性处理
（`python:3.14-slim` 与开发环境同大版本、补 `libgomp1` 解决 pandas 的 OpenMP 依赖、
先拷 requirements 再拷代码以命中层缓存、`.dockerignore` 避免把宿主 `.venv` 打进镜像、
compose 用 `${VAR:-默认}` 而不是 `env_file` 以保证 `.env` 缺失也能启动）。
**请在你的机器上跑一次 `docker compose up --build` 确认**。

**真实大模型链路实测**（模型 `deepseek-v4-flash`）：

```text
task_id = task_ad8e217d711e44b4   最终状态: completed
--- 执行链路 ---
  query_order      success  入参={'order_id': 'PO20260928-00002'}
  query_logistics  success  入参={'order_id': 'PO20260928-00002'}
  safety_gate      success  入参={'action_type': 'query',   'order_id': 'PO20260928-00002'}
  safety_gate      success  入参={'action_type': 'suggest', 'order_id': 'PO20260928-00002', 'sku': 'CC-KDLX011', 'qty': 8}
--- trace --- {'total_calls': 2, 'success_rate': 1.0, 'denied_count': 0, 'avg_latency_ms': 743.1}
```

即：模型自主完成了两轮工具调用（查订单 + 查物流），产出完整的自然语言处理建议，
安全闸门判定正常落库，**服务端日志中 LLM 调用失败次数为 0**。

**上传文件真正驱动查询的判定性验证**（自动化测试里的核心用例）：

用一份"默认数据源里根本不存在的订单"跑两次对比 ——

| 场景 | 闸门参数 | 说明 |
|---|---|---|
| 不带附件 | `inventory_note="订单 PO20260929-88888 无法定位，需人工核对"` | 默认数据源查不到 |
| 带 `delivery` 附件 | `params={'order_id':..., 'sku': 'XC-JLY008'}`，闸门放行 | **从上传统计的订单里取到了 SKU** |

两次结果不同才证明上传的数据真的被用上（只看"返回 200"是证明不了任何事的）。
同一用例还验证了**任务级数据源用完后被还原**（下一个任务查同一个订单又变回"无法定位"）。

**上传临时目录清理实测**：任务处理完后残留 0 个文件。

---

## 18. 几个刻意的设计取舍

1. **接口只收单，绝不在这里跑 Agent。** 提交接口做四件事：落文件 → 建任务 → 入队 → 返回。
   Agent 是同步阻塞的（调模型 + 读文件），在请求里跑会把事件循环卡死几十秒。
2. **`asyncio.to_thread` 而不是把 Agent 改成 async。** Phase 1 的 Agent 与 4 个业务工具
   都是同步实现（openai 同步客户端、pandas 读文件），改 async 等于重写整条链路，
   风险远大于收益。`to_thread` 是标准做法。
3. **执行阶段串行，并把这个约束写在代码里。** 不假装支持并行：任务级数据源要临时改写
   全局单例，Agent 内部也依赖若干进程内单例。`WORKERS>1` 目前只在"取任务"这层并行，
   Worker 里用 `threading.Lock` 保证执行串行。要真正并行得先把 Agent 改成无状态。
4. **取消是协作式的，并且说清最坏延迟。** Python 无法从外部硬杀正在运行的函数，
   所以检查点放在"每轮 LLM 调用之前"（这是能插入的最细粒度），
   最坏延迟 = 当前这一步耗时。接口用 `message` 明确告知用户属于哪种情况，
   而不是含糊地回一句"已取消"。
5. **Worker 兜底任务终态。** 真 Agent 会在 `run()` 内部落 cancelled；
   Worker 里仍保留一次 `only_if_open` 兜底 —— 保证"任何路径抛了 TaskCancelled 但没落库"
   时，任务也不会永远停在 processing（这是写测试时被假 Agent 暴露出来的真实健壮性问题）。
6. **上传文件必须做"任务级数据源"，且用完还原。** 直接往全局单例里塞，A 客户上传的订单
   会串到 B 客户的任务里。这是数据串台级别的缺陷，不是整洁度问题。
7. **错误码机器可读。** 统一 `{"error":{"code","message"}}`，让前端能按 `code` 分支
   （如 `payload_too_large` 提示压缩文件），而不是正则匹配中文提示语。
8. **`.dockerignore` 是必需品不是可选项。** `COPY . .` 完全无视 `.gitignore`，
   没有它会把手宿主机几百 MB 的 Windows `.venv` 和历史数据库一起打进镜像。
9. **测试默认不碰真实 API。** `AGENT_FORCE_MOCK=1` 写进 `conftest.py`，
   保证 `pytest` 可复现、免费、离线可跑。
