# 智能工单售后 Agent —— 总体交付报告（Phase 1 + Phase 2）

> 项目根：`D:\工作区\AI-Ticket-Operations-Agent`
> 报告日期：2026-09-30（Docker 验收于当日 13:50–14:00 补完）
> 覆盖范围：Phase 1 持久化层、Phase 2 Web 服务化、Docker 容器化（含今日环境落地与端到端验收）

---

## 0. 一句话结论

**三个阶段全部闭环。** Phase 1 持久化层、Phase 2 Web 服务化（113 项测试全绿），
以及最后一块拼图 **Docker 容器化 —— 镜像构建 + 容器运行 + 端到端接口验收全部通过（41/41）**。
验收过程还额外证明了两个关键生产特性：**容器销毁重建后审计记录依然留存**
（SQLite 落在宿主机挂载目录）与**多用户并发提交互不阻塞**（4 单 0.30s 内全部收单）。

| 阶段 | 状态 |
|---|---|
| Phase 1 持久化层 | ✅ 完成，88 项测试通过 |
| Phase 2 Web 服务化 | ✅ 完成，113 项测试通过，真实 uvicorn 端到端实测 |
| Docker 安装包下载 | ✅ 完成并校验（签名 Valid） |
| Docker Desktop 安装 | ✅ 完成，引擎 29.8.1 (Docker Desktop 4.93.0) 已就绪 |
| **Docker 镜像构建** | ✅ `ai-ticket-operations-agent:phase2` 构建成功（663 MB） |
| **容器运行 + 端到端验收** | ✅ **41/41 项通过**，容器 healthy，生产模式 CMD 亦验证通过 |

---

## 1. 交付物总览

### 1.1 代码规模（本次两个阶段累计）

| 分组 | 文件数 | 行数 |
|---|---|---|
| Phase 1 持久化层 | 7 | 1,241 |
| Phase 2 API 包 | 10 | 1,529 |
| Phase 2 脚本 / 容器化 | 5 | 351 |
| Phase 2 测试 | 1 | 399 |
| 被改造的既有文件 | 3 | 1,169 |
| **合计** | **26** | **4,689** |

### 1.2 文件清单

**Phase 1 新增**
```
src/persistence/__init__.py    包出口
src/persistence/database.py    引擎 / 会话工厂 / get_db() / init_db()
src/persistence/models.py      ORM：Task + ToolExecution
src/persistence/crud.py        全部数据库读写函数
src/query_cli.py               历史查询 CLI（recent / task / date / stats）
tests/conftest.py              测试期数据库重定向（不污染真实审计库）
tests/test_persistence.py      17 项持久化测试
```

**Phase 2 新增**
```
src/api/__init__.py
src/api/settings.py            pydantic-settings 配置 + DEEPSEEK_* → MODEL_* 别名映射
src/api/models.py              Pydantic 请求/响应模型 + ORM→响应转换
src/api/deps.py                依赖注入（get_db / get_worker / get_settings）
src/api/dataset.py             上传文件 → 临时目录 → 任务级数据源
src/api/worker.py              asyncio.Queue + 后台 Worker + 协作式取消
src/api/main.py                FastAPI 实例 / CORS / 全局异常处理 / lifespan
src/api/routers/__init__.py
src/api/routers/system.py      /health、/metrics
src/api/routers/tasks.py       submit / detail / recent / cancel
scripts/run_api.py             启动脚本（--host / --port / --reload）
scripts/acceptance_docker.py   ★ 容器化验收脚本（41 项端到端检查，可重复运行）
Dockerfile                     python:3.14-slim + libgomp1 + curl
docker-compose.yml             单服务编排
.dockerignore                  防止宿主 .venv / 审计库被打进镜像
.env.example                   环境变量样板（兼容两套命名）
tests/test_api.py              25 项接口测试
```

**被改造的既有文件**
```
src/agent.py                   Phase 1 落库 + Phase 2 外部 task_id / 协作式取消
src/models/client.py           修复思考模式 reasoning_content 未回传的缺陷
src/persistence/crud.py        追加 get_tasks_page / count_tasks
requirements.txt               追加 sqlalchemy / fastapi / uvicorn 等
tests/conftest.py              追加 AGENT_DB_PATH + AGENT_FORCE_MOCK 隔离
.gitignore / README.md         同步更新
```

---

## 2. Phase 1 —— 持久化层

### 2.1 目标
给 Agent 装「长期记忆」：每次任务执行自动落 SQLite，支持历史查询与审计追溯。

### 2.2 必要的结构纠偏（需求书与真实仓库有偏差）

| 需求书的写法 | 仓库真实情况 | 处理 |
|---|---|---|
| 目录 `AI-Order-Operations-Agent` | 实为 `AI-Ticket-Operations-Agent` | 按真实目录落地 |
| 工具 `data_quality_check` / `order_audit` / `generate_anomaly_report` / `generate_delivery_order` | 实为 `query_order` / `query_logistics` / `check_inventory` / `create_suggestion` | 按真实工具落地 |
| `tests/test_order_workflow.py` | 实为 `tests/` 下 11 个文件、71 项用例 | 保持全绿 |
| 新建 `src/models.py` | `src/models/` 已是 LLM 接入层包，**会冲突** | 改放 `src/persistence/models.py` |
| `sqlalchemy==2.0.36` | 项目 venv 是 Python **3.14.7**，2.0.36 解析 `Mapped[str \| None]` 必崩 | 放宽为 `>=2.0.36,<2.1.0`（实测 2.0.54） |

> 关于 SQLAlchemy：2.0.36 在 Python 3.14 上会抛
> `TypeError: descriptor '__getitem__' requires a 'typing.Union' object but received a 'tuple'`。
> 若运行环境是 Python 3.11，原定的 2.0.36 同样可用。

### 2.3 关键设计取舍

1. **持久化是增强项，不是启动前提。** 未装 sqlalchemy 时自动降级为「不落库」并打 WARNING；
   所有写库动作都包在 `try-except` 里，写库失败只记 ERROR 日志，不打断工单处理。
2. **失败与被拦截同样留痕。** 工具报错记 `failed` + 原因；被安全闸门拒绝记 `blocked` + 原因。
   审计最关心的恰恰是「当时为什么没成」。
3. **`allowed=False` 与「待人工确认」严格区分。** 前者是闸门拒绝 → `blocked`；
   后者是闸门放行、等人工审批 → `success`（本次闸门调用本身成功，`output_result.requires_approval=true`）。
4. **超长结果截断在字符串层**（10000 字符 + `…[结果已截断]`）。这是刻意取舍：
   截断后不再保证是合法 JSON，但「看到前 10000 字」对审计比「格式完整却什么都没存」更有用。
5. **外键真的生效。** SQLite 默认不强制外键，已在每条连接上打开 `PRAGMA foreign_keys=ON`，
   杜绝 `tool_executions` 出现指向不存在任务的孤儿记录。
6. **测试不污染审计库。** `tests/conftest.py` 把库路径重定向到临时目录，实测跑完测试真实库任务数不变。
7. **`task_id` = `task_` + uuid4 前 16 位**（21 字符）。兼顾唯一性与「日志里可读、能口头传递」。

---

## 3. Phase 2 —— Web 服务化

### 3.1 目标
把命令行 Agent 变成 HTTP 服务：多人提交工单 → 异步处理 → 查询进度 → 取消任务。

### 3.2 对外接口清单（取自 `/openapi.json`，OpenAPI 3.1.0）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/tasks/submit` | 提交任务（异步处理，立即返回 task_id） |
| GET | `/api/v1/tasks/{task_id}` | 查询任务详情 + 全部工具执行记录 |
| GET | `/api/v1/tasks/recent` | 最近任务列表（支持 `limit` / `offset` 分页） |
| POST | `/api/v1/tasks/{task_id}/cancel` | 取消任务 |
| GET | `/health` | 健康检查（内部会探一次数据库） |
| GET | `/metrics` | 运行指标（队列长度 / 运行中 / 已取消等） |

访问 `http://localhost:8000/docs` 可看到自动生成的交互式文档。

### 3.3 异步架构

```
用户 POST /api/v1/tasks/submit
    ↓
保存上传文件到临时目录（tempfile）
    ↓
创建 Task 记录（status=processing）+ 返回 task_id（毫秒级）
    ↓
task_id + 数据集 入 asyncio.Queue
    ↓
后台 Worker 从队列取出
    ↓
Agent.run() 在 asyncio.to_thread() 中执行（同步函数不阻塞事件循环）
    ↓
completed / failed / cancelled 写回数据库
```

### 3.4 关键工程决策

1. **接口只收单不干活。** 实测 submit 接口耗时 **95 ms** 返回 `processing`，任务在后台约 2 s 完成。
   Agent 用 `asyncio.to_thread` 丢线程池，避免把同步函数改 async 带来的风险。
2. **执行阶段串行。** 任务级数据源要临时改写全局单例，Worker 用 `threading.Lock` 串行执行，
   保证不同任务的数据源不互相串台。
3. **上传文件真的驱动查询**（不是摆设）：执行窗口内把
   `src.data_source.data_source` 与 `src.tools.business_tools.data_source` **两处模块属性**
   都换成任务级实例，`finally` 还原。
   > ⚠️ 必须换两处：`agent`/`gate`/`llm_suggestion` 是函数内惰性 import（改模块属性即生效），
   > 而 `business_tools` 在模块顶层 `from ... import data_source`，把名字绑进了自己的命名空间。
4. **取消是协作式的**：排队中 → 立刻取消；执行中 → 置 `threading.Event`，
   Agent 到检查点抛 `TaskCancelled` → 落 `cancelled` 终态并返回结构化结果（不抛异常给调用方）。
   Worker 里再留一次 `only_if_open` 兜底，保证终态一定落地。
5. **`asyncio.Queue` 每次 `start()` 重建**：它绑定「第一次使用它的事件循环」，
   跨事件循环复用会抛 `RuntimeError`（测试里反复启停服务必踩）。
6. **错误统一为** `{"error": {"code", "message"}}`，`code` 机器可读。
   另注意路由顺序：`/recent` 必须写在 `/{task_id}` 之前，否则会被当成 task_id。
7. **FastAPI 表单字段名 `return` 是 Python 关键字** → 参数名用 `return_order`，配 `File(alias="return")`。

### 3.5 ★ 顺带修复的 Phase 1 遗留缺陷（重要）

`deepseek-v4-flash` 是**思考模式**模型，返回 `tool_calls` 时会附带 `reasoning_content`，
**下一轮必须原样回传**，否则服务端判 400：
`The reasoning_content in the thinking mode must be passed back to the API.`

- **现象**：第一轮工具调用成功、**第二轮必失败**。
- **为什么难查**：`agent.py` 主循环有 fail-safe（LLM 异常 → 降级「升级人工」、不抛异常），
  所以表象像「模型自己不想干活」，而不是「请求被服务端拒绝」。
- **修法**：`LLMClient.chat_with_tools` 把思维链存到实例属性
  （**返回值仍是 `(content, tool_call)` 二元组**，不破坏既有调用方与测试桩），
  `agent.py` 用 `getattr(self.llm, "last_reasoning_content", None)` 取用并写进 assistant 消息。
- **修复后实测**：2 轮工具调用（query_order + query_logistics）全部成功，LLM 失败数 **0**。

---

## 4. Docker 部分

### 4.1 今日完成的环境落地

| 项 | 结果 |
|---|---|
| 官方安装包下载 | ✅ `C:\Users\libiu\Downloads\Docker Desktop Installer.exe`（627,791,792 字节，598.7 MB） |
| 完整性校验 | ✅ 大小与官方 `Content-Length` 完全一致；PE 头 `MZ` |
| 数字签名 | ✅ **Valid**，签名者 `CN=Docker Inc, O=Docker Inc, Palo Alto, California` |
| SHA256 | `C139124C9CF71477DC565C3C0EA5A18F90B93D68EBE9AAA848A065960416C0BC` |
| Docker Desktop 安装 | ✅ 用户级安装于 `%LOCALAPPDATA%\Programs\DockerDesktop\` |
| Docker CLI | ✅ **29.8.1**（build 4a63305，windows/amd64） |
| Docker Compose | ✅ **v5.5.1** |

> 下载过程有个坑：`desktop.docker.com` 在本机网络下**连接极不稳定**，每次刚下 1~9 MB 就被掐断
> （`schannel: server closed abruptly`）。而且 `curl -I` 测 Range 会**误判**为不支持续传
> （返回 200），换**真实 GET** 才看到 `HTTP 206` —— 服务端其实支持断点续传。
> 最终用「循环 + `curl -C -` 逐段累加」下完（不能加 `--retry`，它会和 `-C -` 打架反而从 0 重来）。

### 4.2 静态校验（构建前的预检）

| 检查项 | 结果 |
|---|---|
| `docker compose config` 解析 | ✅ 通过，变量替换与卷挂载均正确展开 |
| 基础镜像 `python:3.14-slim` 存在性 | ✅ 真实存在（linux/amd64），且镜像仓库**网络可达** |
| Dockerfile 系统依赖 | ✅ 已装 `libgomp1`（pandas OpenMP 运行时）+ `curl`（healthcheck 用） |
| `.dockerignore` | ✅ 已排除 `.venv/`、`data/*.db`、`.env*`、`logs/`、`outputs/`，保留 `data/mock/` |
| 关键文件齐全度 | ✅ Dockerfile / compose / .env.example / .dockerignore / run_api.py 全在 |

### 4.3 引擎启动

Docker Desktop 由用户双击启动并接受服务条款后，引擎就绪：

```
Server: Docker Desktop 4.93.0 (240920)
 Engine:  Version 29.8.1  API 1.56  OS/Arch linux/amd64
 containerd v2.3.5 · runc 1.5.1
```

> **踩坑复盘**：上一轮我在沙箱内启动 Docker Desktop 失败，报
> `rename ...sailor-ingest.sock: Access is denied` 与
> `wsl.exe: Access is denied`。两个原因都是**沙箱安全策略**（禁用 `wsl.exe`、禁改 Docker 自己的 socket），
> **不是 Docker 装坏了** —— 用户在沙箱外双击启动，一次成功，印证了这个判断。
> 另一个现实约束：Git Bash 会把 `docker exec ... /app/data` 里的**容器内绝对路径自动改写成 Windows 路径**，
> 必须加 `export MSYS_NO_PATHCONV=1` 才能正确执行容器内命令。

### 4.4 镜像构建

```
docker compose build
→ Image ai-ticket-operations-agent:phase2  Built   (663 MB / 662.9 MB 去重后)
```

| 检查项 | 结果 |
|---|---|
| 基础镜像 `python:3.14-slim` 拉取 | ✅ 成功 |
| `apt-get install libgomp1 curl` | ✅ 成功（容器内 `ldconfig -p` 可见 `libgomp.so.1`） |
| pip 装依赖 | ✅ 42 个包，含 pandas 3.0.6 / numpy 2.5.3 / sqlalchemy 2.0.54 / fastapi 0.142.1 |
| 层缓存顺序 | ✅ 先 `requirements.txt` 后 `COPY . .`，改代码不触发重装 pandas |
| 构建耗时 | 约 **4 分 45 秒**（几乎全在下载 numpy/pandas 的 wheel） |

容器内环境自检：
```
$ docker exec ai-ticket-agent-api python -c "import pandas, numpy, sqlalchemy, fastapi; ..."
pandas 3.0.6 · numpy 2.5.3 · sqlalchemy 2.0.54 · fastapi 0.142.1
```

### 4.5 容器运行

```
$ docker compose up -d
$ docker compose ps
NAME                  IMAGE                               STATUS
ai-ticket-agent-api   ai-ticket-operations-agent:phase2   Up 17 seconds (healthy)   0.0.0.0:8000->8000/tcp
```

启动日志确认三件事：**DB 落在挂载目录**、**mock=False（真调模型）**、**Worker 已拉起**：

```
INFO | API 启动中：db=/app/data/agent_operations.db
INFO | Worker：Agent 已就绪 mock=False use_llm=True persistence=True
INFO | 后台 Worker 已启动 (workers=1, queue_max=1000)
INFO | API 已就绪：workers=1 mock=False
```

`(healthy)` 是 Docker 自己的 `HEALTHCHECK` 打出来的 —— 它调的是 `/health`，
而 `/health` 内部还会 `SELECT 1` 探一次库，所以「healthy」的含义是**能对外服务**，不只是「进程活着」。

### 4.6 端到端验收（41/41 项通过）

验收脚本：`scripts/acceptance_docker.py`（新增，可重复运行）。

```
python scripts/acceptance_docker.py
…
验收结果：通过 41 项，失败 0 项
```

| # | 用例 | 结果 | 关键证据 |
|---|---|---|---|
| 1 | `/health` 探活 | ✅ | `{"status":"healthy","version":"1.0.0"}`（非 degraded ⇒ 库连通） |
| 2 | 路由齐全 | ✅ | 6 条路由全在（health / metrics / submit / recent / detail / cancel） |
| 3 | **异步不阻塞** | ✅ | submit **0.05s** 返回 `task_id`，Agent 处理在后台 |
| 4 | 无附件任务真调模型 | ✅ | `completed`，5 条流水：query_order / query_logistics / 3×safety_gate |
| 5 | **附件真正驱动查询（正向）** | ✅ | 上传「验收专用订单」，结果里出现**只存在于该文件**的商品名/物流单号 |
| 6 | **附件对照（负向）** | ✅ | 不传附件时查同一订单号，**查不到**该单的独有信息 → 证明数据源真的被替换而非忽略 |
| 7 | 附件留痕 | ✅ | 审计里有 1 条 `file_upload`，记录了文件名 / 大小 / role / 是否驱动查询 |
| 8 | 列表分页 | ✅ | total/limit/offset/items 正确，`offset=1` 生效 |
| 9 | 详情查询 | ✅ | 含完整执行链路（入参 / 结果 / 错误 / 时间） |
| 10 | 404 统一错误壳 | ✅ | `{"error":{"code":"task_not_found",...}}` |
| 11 | 422 入参校验 | ✅ | 缺 `user_input` → `validation_error`，同一错误壳 |
| 12 | **协作式取消** | ✅ | cancel 返回 200 → 状态收敛到 `cancelled`；**重复取消 409** |
| 13 | `/metrics` | ✅ | `queue_size=0 running=0 workers=1 worker_alive=true` + 当日统计 |
| 14 | **审计库落在宿主机挂载目录** | ✅ | 宿主机 `data/agent_operations.db` 里能查到本次提交的 task_id |

### 4.7 额外验证（超出原验收清单）

**① 容器销毁重建后，审计记录依然留存**（这是「SQLite 落在挂载卷」这个设计的真正意义）

```
重建前： curl /api/v1/tasks/task_b5e750ffa05440ed → completed，5 条流水
docker compose down          # 容器与网络全部销毁
docker compose up -d         # 全新容器、全新进程
重建后： curl /api/v1/tasks/task_b5e750ffa05440ed → completed，5 条流水，附件留痕完整
         任务总数 total = 11（与重建前一致）
```

**② 多用户并发提交**（4 个不同 `customer` 会话同时提交）

```
用户A  HTTP 200  task_bf83e66e1ba0472b  响应 0.30s
用户B  HTTP 200  task_d8acb6a4be714597  响应 0.25s
用户C  HTTP 200  task_c16a321840254a6d  响应 0.25s
用户D  HTTP 200  task_c1f75d7a4ab24956  响应 0.24s
4 单总耗时 0.30s；最终 4 单全部 completed（无失败、无丢失）
```

> 价值：证明「提交接口只接单、不跑 Agent」这个设计是对的 —— 否则 4 个请求会串行排队几分钟。

**③ 生产模式 CMD（不带 `--reload`）单独验证**

```
docker run -d --name agent-prod-test -p 8100:8000 --env-file .env ai-ticket-operations-agent:phase2
→ /health 返回 {"status":"healthy"}；进程 1 命令行确认无 --reload，单进程无 reloader
```

**④ 优雅停机与临时文件清理**

```
docker compose stop
→ INFO | 后台 Worker 已停止
→ INFO | API 已关闭
→ INFO | Application shutdown complete.
→ 容器 Exited (0)

容器内 /tmp 下 task-* 残留：0 个；/app/uploads 不存在
```

**⑤ 磁盘占用**

| 项 | 占用 |
|---|---|
| 镜像 `ai-ticket-operations-agent:phase2` | 662.9 MB |
| 构建缓存（可回收 4 MB） | 667.9 MB |
| 宿主机剩余 | C: 21 GB / D: 320 GB |

---

## 5. 验收矩阵

| # | 验收项 | 状态 | 证据 |
|---|---|---|---|
| 1 | Phase 1：入口正常运行、功能不变 | ✅ | `scripts/run.py`、`python src/agent.py` 均可用 |
| 2 | Phase 1：任务后自动生成 `data/agent_operations.db` | ✅ | 删库后跑一次即重建（`init_db()` 幂等建表） |
| 3 | Phase 1：库里有 1 条 task + N 条 tool_execution | ✅ | 实测 task(completed) + 1~4 条流水 |
| 4 | Phase 1：`query_cli` 四个子命令 | ✅ | `recent` / `task` / `date` / `stats` 全通 |
| 5 | Phase 1：原有 71 项测试 | ✅ | 88 passed（71 原有 + 17 新增） |
| 6 | Phase 2：`uvicorn src.api.main:app` 正常启动 | ✅ | 真实启动，`/health` 返回 healthy |
| 7 | Phase 2：`/docs` 可访问 | ✅ | HTTP 200 |
| 8 | Phase 2：submit 返回 task_id | ✅ | 实测 **95 ms** 返回 `processing` |
| 9 | Phase 2：任务详情含工具执行记录 | ✅ | 每条含入参 / 结果 / 错误 / 时间 |
| 10 | Phase 2：`/recent` 分页列表 | ✅ | total / limit / offset / items 齐全 |
| 11 | Phase 2：异步不阻塞 HTTP | ✅ | submit 95 ms，处理在后台完成 |
| 12 | Phase 2：上传文件真正驱动查询 | ✅ | 用「默认数据源里不存在」的订单对比：不带附件被拦、带附件取到 SKU；用后已还原 |
| 13 | Phase 2：上传临时文件用后清理 | ✅ | 残留 0 个 |
| 14 | Phase 2：真实大模型多轮工具调用 | ✅ | query_order + query_logistics 两轮全通，LLM 失败数 0 |
| 15 | Phase 2：113 项测试全过 | ✅ | `113 passed in 53.50s` |
| 16 | Phase 2：向后兼容 | ✅ | `src/agent.py`、`scripts/run.py`、`query_cli.py` 均仍可用 |
| 17 | Docker：`docker compose config` 合法 | ✅ | 解析通过 |
| 18 | Docker：`python:3.14-slim` 可用 | ✅ | 真实拉取成功，镜像构建完成 |
| 19 | **Docker：`docker compose up --build` 一键启动** | ✅ | 镜像 663 MB 构建成功，容器 `Up (healthy)` |
| 20 | **Docker：容器内 API 端到端验证** | ✅ | **41/41 项通过**（见 4.6） |
| 21 | Docker：容器销毁重建后记录留存 | ✅ | `down` → `up`，同一 task_id 的 5 条流水完整，总数不变 |
| 22 | Docker：多用户并发提交 | ✅ | 4 单 0.30s 内全部收单并全部完成 |
| 23 | Docker：生产模式 CMD（无 `--reload`） | ✅ | 单进程，/health healthy |
| 24 | Docker：优雅停机 + 临时文件清理 | ✅ | `Exited (0)`，Worker 已停止，`/tmp` 残留 0 |
| 25 | Docker：后端不串数据 | ✅ | 并发任务与上传件均按任务隔离，用后还原数据源 |

**25 / 25 项验收全部通过。**

---

## 6. 待办事项

### 6.1 需要你做的

**无。** 本次任务已全部闭环。容器当前处于运行状态（`docker compose start` 已恢复），
可直接访问 <http://127.0.0.1:8000/docs> 交互式试用。

日常运维命令：

```bash
docker compose up -d          # 启动（-d 后台）
docker compose logs -f api    # 看实时日志
docker compose stop           # 停止（保留容器与数据）
docker compose down           # 销毁容器（data/ 里的审计库不受影响）
docker compose down -v        # 连同网络/卷一起清（审计库仍在宿主机 data/）
```

### 6.2 后续可选的加固建议
- **镜像存放位置**：C 盘仅剩 21 GB，D 盘有 320 GB。建议在 Docker Desktop 设置里把
  「Disk image location」改到 D 盘，避免后续镜像把 C 盘吃满。
- **密钥可见性**：`docker compose config` 会把 `.env` 里的真实 key 展开到 compose 配置里
  （本次输出中可见 `MODEL_API_KEY` / `DEEPSEEK_API_KEY` 明文）。这是 compose 的正常行为，
  `.env` 已被 gitignore；但**不要把 `docker compose config` 的输出贴到公开场合**。
- **生产部署**：`compose` 当前挂了 `--reload`（开发模式，挂载了 `./src`）；
  生产请删除 `command:` 与 `volumes:` 里除 `./data` 外的挂载，改用镜像内置 CMD。
  另外 Dockerfile 默认以 root 运行，Linux 生产环境建议启用文件末尾注释里的非 root 用户。
- **横向扩容**：当前是单进程 + 进程内 `asyncio.Queue`（`worker.py` 里的执行锁保证了
  「任务级数据源临时替换」不会串台，但这也意味着**必须单进程**）。要多副本时需换外部队列
  （Redis 等）与外部数据库；`src/persistence/` 这层已是标准依赖注入形态，可直接复用。
- **清理构建缓存**：`docker builder prune` 可回收约 668 MB（当前 build cache 全为可回收）。
