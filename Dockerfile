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
