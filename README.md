# AI-Ticket-Operations-Agent

企业级智能工单 / 售后运营 Agent —— 给企业内部客服/运营团队用的工单处理助手。

## 一句话定位

消费者反馈的问题进入系统后，Agent 自动查单、审核、给出处理建议；敏感操作（退款/补发等）必须通过代码级安全闸门与人工二次确认；全程留痕可审计；异常自动升级人工兜底。

## 设计原则

1. **安全优先**：敏感动作永远由代码 + 人工共同控制，不依赖大模型自觉。
2. **人机协同**：明确"什么自动做、什么必须人审、异常如何兜底"。
3. **可评测**：评测不是事后动作，而是随每次工具调用落指标。
4. **多模型接入**：模型是插件不是绑定，支持多模型对比。

## 目录结构

```
AI-Ticket-Operations-Agent/
├── config/            # 配置（本地无密钥配置）
├── data/
│   ├── mock/          # 模拟数据 CSV（订单/库存/退货）
│   └── agent_operations.db  # SQLite：业务表 + 运行台账（gitignore）
├── docs/              # 架构与说明
├── logs/              # 运行时日志（gitignore）
├── scripts/           # 入口脚本（run.py / run_api.py / migrate_csv_to_db.py）
├── src/
│   ├── api/           # Web 服务层（Phase 2）：FastAPI 路由 / 异步队列 / 上传数据集
│   ├── domain/        # 领域模型：工单/订单/退货/动作/建议/GateOutcome
│   ├── models/        # 模型接入层（多模型 OpenAI 兼容）
│   ├── tools/         # 工具层：查单/物流/库存/建议/摘要
│   ├── safety/        # 安全层：白名单/闸门/二次确认/业务相对约束
│   ├── execution/     # 执行层：已批准动作的落地（幂等 + 执行前复核）
│   ├── memory/        # 记忆层：会话记忆（按 session 分桶） + 用户画像 + FTS5 案例检索（RAG）
│   ├── persistence/   # 持久化层：SQLite（Task/ToolExecution 台账 + 订单/库存/退货业务表）
│   ├── observability/ # 可观测：trace + 日志
│   ├── evaluation/    # 评测层：评测集/执行/指标
│   ├── agent.py       # 编排层（LLM + 工具调用主循环）
│   ├── config.py      # 全局配置
│   ├── data_source.py # 数据源层：DB 优先 + CSV 自动降级
│   ├── logger.py      # 日志
│   └── query_cli.py   # 历史记录查询 CLI（recent / task / date / stats）
├── Dockerfile         # 容器镜像（Phase 2）
├── docker-compose.yml # 一键启动（Phase 2）
└── tests/             # 测试（113 项）
```

## 快速开始

```bash
# 1. 建虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 2. 装依赖
pip install -r requirements.txt

# 3. (可选) 配置模型密钥，启用真实 LLM 建议
copy .env.example .env   # 填入 MODEL_API_KEY / MODEL_BASE_URL / MODEL_NAME

# 3.5 迁移业务数据到 SQLite（可选，但强烈建议 —— 查询走索引，快很多）
#     不迁也能跑：库文件不存在时数据层自动降级读 CSV
.venv\Scripts\python.exe scripts\migrate_csv_to_db.py --reset

# 4. 运行
.venv\Scripts\python.exe scripts\run.py        # mock 模式（无需 key）
.venv\Scripts\python.exe scripts\run.py --llm  # 真实 LLM 模式（需 .env）

# 5. 起 Web 服务（Phase 2）
.venv\Scripts\python.exe scripts\run_api.py    # http://127.0.0.1:8000/docs
# 或者容器一键启动
docker compose up --build
```

## 数据层（CSV → SQLite 迁库）

查询类工具（查单/物流/库存/退货）支持**两条数据通路**，对上层完全同构：

| 通路 | 实现 | 何时启用 |
|---|---|---|
| SQLite | `DatabaseDataSource` | `data/agent_operations.db` 存在（推荐） |
| CSV | `DataSource` | 库文件不存在时自动降级，服务照常可用 |

选路由 `src/data_source.py::build_data_source()` 统一决定，**降级是默认安全行为**：
本地不建库也能跑，服务器上建好库自动提速，两种情况都不用改代码。

### 一次性迁移

```bash
python scripts/migrate_csv_to_db.py --reset   # 从 CSV 全量重建业务表
python scripts/migrate_csv_to_db.py --dry-run # 只看会迁多少，不写库
```

原始 CSV（`data/mock/`，已内置无需下载）：
- `orders.csv`  —— 订单明细（5000 行，一单多 SKU，含真实比例脏数据）
- `inventory.csv` —— 门店库存（5000 行）
- `returns.csv` —— 售后退货（5000 行）

迁移会**清洗脏数据**（空订单号/空商品/空门店/数量非正）、把一单多 SKU 聚合成
`items` JSON 字段、按（门店, SKU）累加多仓库存。清洗规则与 CSV 数据源完全一致，
保证迁移前后"看到的数据"不变（实测：订单 2432 条、库存 225 条、退货 5000 条）。

### 三张业务表

| 表 | 主键 | 说明 | 索引 |
|---|---|---|---|
| `orders` | `order_id` | 一单一行，明细在 `items`（JSON） | `order_id`、`customer`、`store`、`created_at` |
| `inventory` | 自增 `id` | 一行 = 一个（门店, SKU）快照 | `(store, sku)` 联合唯一 |
| `returns` | 自增 `id` | 一行 = 一笔售后申请 | `order_id`、`customer`、`created_at` |

与运行台账（`tasks` / `tool_executions`）**共存于同一个 SQLite 文件**，
但语义独立：台账记"Agent 做了什么"，业务表是"业务事实长什么样"。

### 新增查询能力（CSV 时代做不到）

```python
ds = data_source                       # 自动选中 DB 或 CSV

ds.get_returns(customer="华洋", days=None)      # 查退货记录
ds.get_customer_return_rate("华洋", days=None)  # 某客户退货率 = 退过货的订单 / 总订单
ds.search_orders(customer="华洋", store="苏州")  # 按客户/门店模糊查
```

`days=None` 表示不限时间窗。注意迁移进来的演示数据时间在 2026-09 ~ 2027-12
（相对"今天"是未来日期），此时 `days=30` 这类窗口几乎筛不到东西，看全量请显式传 `None`。

**退货率的计算口径**：以**订单**为中心 —— 分母是该客户期内订单数，分子是这些订单里
"至少有一笔退货"的去重订单数。不按退货表的「收货人」字段直接数，因为实测数据显示
退货表的收货人是脱敏代号（`收货人80`），与订单表的真实姓名（`华洋`）**交集为空**；
而退货表的「原订单号」100% 能关联到订单。

### 选一条 SQL 验一下

```sql
-- 验收：查历史订单
SELECT * FROM orders WHERE order_id = 'PO20260928-00001';

-- 联表演示：某客户的下单数与退货单数
SELECT o.customer,
       COUNT(DISTINCT o.order_id) AS order_count,
       COUNT(DISTINCT r.order_id) AS return_count
FROM orders o LEFT JOIN returns r ON o.order_id = r.order_id
WHERE o.customer = '华洋'
GROUP BY o.customer;
```

## 两种运行模式

| 模式 | 触发 | 说明 |
|---|---|---|
| mock | 无 key / `--llm` 未传 | 规则生成建议 + 安全闸门，无需 API，可演示完整流程 |
| 真实 LLM | `--llm` 且已配 key | LLM 生成处理建议 + 工具调用，多模型可切换 |

## AI 能力 · RAG 历史案例检索

让 Agent 处理新工单时，先检索**历史上相似工单的处理方案与结果**，把 top-k 条案例
注入 system prompt —— 建议从"规则版"升级为"案例驱动"。

```
历史工单（问题+建议+结果） --写入--> SQLite FTS5 全文索引
                                          |
新工单（诉求原文） --MATCH--> bm25 排序 --+--> top-2 案例 --> 注入 prompt
```

**技术选型：SQLite FTS5（零新依赖）**
- 索引引擎：SQLite 内建的 **FTS5 全文检索**，虚拟表 `ticket_cases`，
  与业务表（orders / inventory / returns）**共存于同一个库文件** `data/agent_operations.db`
- 分词：`unicode61` 分词器 + **应用层中文 bigram 切词**（见下方说明）
- **不引入任何第三方依赖**：不需要 chromadb / torch / 模型文件，离线环境开箱可用

**★ 中文分词的坑（为什么需要 bigram）**
FTS5 内置的 `unicode61` 分词器**不切中文** —— 它会把整句「客户反馈少发一瓶玻璃水」
当成一个 token，导致 `MATCH '玻璃水'` 永远命中不到。解法是**写入和查询两侧都做
bigram 切词**（相邻两字）：`玻璃水 -> 玻璃 璃水`。两侧用同一个函数是正确性的前提。
此外还维护了一张**领域同义词表**，把口语说法（碎了 / 坏了）扩展到标准用词（破损），
以此弥补"纯关键词检索不做语义泛化"的短板。

**启用与配置**（`.env`，详见 `.env.example`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `RAG_ENABLED` | `true` | 总开关。设为 `false` 时行为与加 RAG **之前完全一致** |
| `RAG_TOP_K` | `2` | 注入 prompt 的案例条数 |

也可在构造 Agent 时显式覆盖：`TicketAgent(rag_enabled=False)`。

**案例自动积累**
每次 `run()` 正常结束后，会自动把这次的「问题 + 建议 + 闸门判定结果」沉淀进案例库
（见 `TicketAgent._index_case`）。所以 Agent 用得越久，可参考的案例越多。
执行结果取自 `gate_outcomes`（闸门真实判定），而不是"Agent 自称成功" ——
与本项目评测层同一原则。同一 `task_id` 重复处理会**覆盖**旧案例（upsert 语义），
不会堆积重复项。

**★ 降级是默认行为（重要）**
RAG 是**增强项**，不是前提条件。以下任一情况都只会打 WARNING 并让检索返回空，
Agent 照常走原来的规则流程，不会因此报错：

- SQLite 版本过老、未编译 FTS5 模块（`available=False`）
- 库文件损坏或不可写
- 查询词切完为空（纯符号）

案例表不存在时会**自动创建**；`RAG_ENABLED=false` 时完全跳过检索与沉淀。

**演示**

```bash
python scripts/demo_fts.py          # 写入 3 条案例并跑 6 个查询验证召回
python scripts/demo_fts.py --keep   # 保留已有案例，仅追加
python scripts/demo_fts.py --agent  # 额外跑一遍 Agent 端到端，看案例注入
```

**接口返回**
`run()` 的结果里新增 `rag_cases` 字段（本次命中的案例及相似度），
Web 层提交任务后可在响应中看到 RAG 是否生效：

```json
{"rag_cases": [{"ticket_id": "task_xxxx",
                "description": "客户反馈少发了一瓶玻璃水...",
                "suggestion": "核实发货记录后补发同款 1 瓶。",
                "outcome": "成功执行：已补发",
                "metadata": {"issue_type": "少发", "store": "苏州工业园店"},
                "score": 0.4523}]}
```

## 业务规则 · 退货风险升级

让 Agent 在处理工单时**先翻这个客户的退货老账**：退得太凶就不许自动处理，转人工。
规则实现在 `TicketAgent._check_return_risk`，由 `_build_suggestions` 在生成建议前调用。

**三条规则**（阈值全部可在 `.env` 调整）

| # | 条件 | 动作 |
|---|---|---|
| 1 | 客户近 30 天退货率 ≥ `RETURN_RISK_THRESHOLD_CUSTOMER`（默认 `0.4`） | **升级人工**：跳过正常建议流程，建议正文直接是风控结论 |
| 2 | 客户近 30 天退货订单数 ≥ `RETURN_RISK_MIN_COUNT`（默认 `2`） | **高风险**：建议里追加提示，动作强制走人工确认 |
| 3 | 订单主商品近 30 天退货率 ≥ `RETURN_RISK_THRESHOLD_SKU`（默认 `0.3`） | **提示高风险商品**：建议里加"建议验货后处理" |

规则 2/3 命中时，该单的所有动作会被**强制抬到人工确认**（`requires_approval=True`），
即使原本不是敏感动作 —— 高风险单子让人先过一眼总不吃亏。

**★ 退货率的口径：以订单为中心，不是按退货表的收货人数**

退货表的「收货人」是脱敏代号（`收货人80`），订单表里是真实姓名（`华洋`），
**两个字段没有任何交集**，直接按客户名查退货**永远是 0 条**。
所以正确口径是：

```
分母 = 该客户近 30 天的订单数
分子 = 这些订单里"至少有一笔退货"的 去重订单数
```

因此本模块**不用** `get_returns(customer=...)` 来数退货次数，而是先用订单号
圈出这个客户的单子，再去筛退货记录（与 `get_customer_return_rate` 内部 JOIN 一致）。
同一订单退多件只算一次（按订单去重），否则"退货率"会超过 1。

**★ 只在 SQLite 数据源上生效（静默降级）**

风控是**增强项**，任何一环出问题都只会打日志并跳过检查，绝不影响工单处理：

| 情况 | 行为 |
|---|---|
| `RETURN_RISK_ENABLED=false` | 完全跳过（行为与加规则之前一致） |
| 客户是访客 / 匿名（`访客`、`游客`、`anonymous`、`unknown`…） | 跳过（没有稳定历史可查） |
| 数据源退回 CSV 模式 | 跳过（CSV 每次要全量解析 2400+ 订单，不适合放在关键路径） |
| 查询过程抛异常 | 捕获后按"无风险"处理，只打 WARNING |

**接口返回**

`run()` 结果新增 `return_risk` 字段（与 `gate_outcomes` 同一处理方式）：

```json
{"return_risk": {
   "checked": true, "escalate": true, "high_risk": true,
   "reason": "客户 魏静 近30天退货率 100%（3/3），达到阈值 40%，建议升级人工审核",
   "notes": [],
   "details": {"customer": "魏静", "customer_rate": 1.0, "return_count": 3,
               "order_count": 3, "sku": "CC-JY004", "sku_rate": 0.6108}}}
```

**⚠️ 阈值标定提醒（重要）**

`0.4 / 0.3 / 2` 是需求给定的默认值，但**对当前演示数据严重偏松**：
演示库整体退货率高达 **87%**（2117/2432 单），按 0.4 会有约 **97%** 的客户被升级人工；
SKU 退货率普遍在 87%~89%，按 0.3 则**几乎每个商品**都会触发"验货"提示。
这会让"升级人工"从例外变成常态，规则失去区分度。

上生产前应基于真实分布重新标定（例如取客户退货率的分位数阈值），
而不是直接沿用演示值。改阈值只需动 `.env`，无需改代码。

## 安全设计要点

**1. 能力与权限分离**：注册给 LLM 的工具只有 3 个只读工具（查单/查物流/查库存）。
退款、补发、改单、作废**故意不注册成工具** —— 模型在能力层面就无法直接发起写操作。

**2. 三重校验**：`SafetyGate.validate` 按「格式 → 范围 → 业务相对约束」依次校验：
- 格式/范围：金额必须为正、退款 ≤ 5000、补发 ≤ 10 件，参数名别名（quantity/money）统一；
- 业务相对约束：**订单必须存在**、**退款不得超过订单实付金额**、**订单状态必须可退款/可补发**。
  例：15 元的订单申请退款 4999 元会被直接拒绝（只校验绝对上限是拦不住这种情况的）。

**3. 失败默认拒绝**：信息不足、参数冲突、订单定位不到 → 一律拒绝或保守转人工。

**4. 执行与批准分离**：`SafetyGate.approve()` 只改状态位；真正的退款/发货由
`src/execution/executor.py` 执行，且要求 `status == "approved"`，带
幂等键 `(工单, 动作, 订单, SKU)` 与执行前复核（审批期间库存被占用会被拦下）。

## 评测方法（重要）

判定依据是**安全闸门的真实行为**（`result["gate_outcomes"]`），而不是
`Action.requires_approval` 这类由代码常量推导的字段。

原因：`requires_approval` 由 `ACTION_RISK` 硬编码为 True，拿它做评测等于断言一个常量。
实测表明，只要判定读这个字段，把闸门改成"全部拒绝"指标也纹丝不动（恒真、无区分度）。

为保证指标可被证伪，`tests/test_evaluation_meta.py` 给评测器本身写了元测试：
- 违规 Agent（直接执行敏感动作）必须被判 FAIL；
- 闸门"全部拒绝"时 normal 用例必须 FAIL；
- 删掉数量上限校验时 A2 必须 FAIL。

## 持久化与历史查询（Phase 1）

Agent 原来只有内存记忆，进程退出即清零。现在每次任务与每次工具调用都会自动落
`data/agent_operations.db`（SQLite），可审计、可追溯。

**两张表**

| 表 | 记录什么 |
|---|---|
| `tasks` | 一次任务（= 一次 `agent.run()`）：用户原始输入、状态（processing / completed / failed）、结果摘要（JSON）、创建/更新时间 |
| `tool_executions` | 任务下的每条流水：真实工具调用 + `safety_gate` 闸门判定；含入参、结果、状态（success / failed / **blocked**）、错误原因、执行时间 |

**设计要点**

- 落库是**增强项而非启动前提**：未装 sqlalchemy 时自动降级为「不落库」并打 WARNING，
  所有写库动作都 `try-except` 包裹，写库失败只记日志，绝不打断工单处理。
- **失败与被拦截同样留痕**：工具报错记 `failed`，被安全闸门拒绝 / 被库存闸门拦下记 `blocked`
  —— 审计最关心的恰恰是「当时为什么没成」。
- 单条输出超过 10000 字符会截断并标注 `…[结果已截断]`。
- 外键真实生效（每条连接开 `PRAGMA foreign_keys=ON`），不会出现指向不存在任务的孤儿流水。
- 测试期数据库通过 `AGENT_DB_PATH` 环境变量重定向到临时目录（见 `tests/conftest.py`），
  跑测试不会把测试数据写进真实审计库。

**查询**

```bash
python src/query_cli.py recent 10              # 最近 10 条任务
python src/query_cli.py task <task_id>         # 某任务详情 + 完整执行链路
python src/query_cli.py date 2026-09-30        # 按日期查询
python src/query_cli.py stats today            # 今日处理量 / 拦截数 / 失败数
```

`tasks` 与 `tool_executions` 是一对多，`task_id` 形如 `task_3f9a1c07be24d5e1`，
每次 `run()` 会在控制台打印，直接拿它去 `query_cli.py task` 即可。

## Web 服务化（Phase 2）

把 Agent 从"命令行工具"变成 HTTP 服务，支持多人并行提交工单、查询进度、取消任务。
**只做后端 API，不含前端页面与登录鉴权。**

### 异步处理架构

关键点是**接口只收单、不干活**：`Agent.run()` 是同步阻塞的（要调大模型、读数据文件），
如果在请求里直接调用，事件循环会被卡住，期间所有请求（包括健康检查）都得排队。

```
POST /api/v1/tasks/submit
   └─ 上传文件落临时目录 → 建任务记录(processing) → 丢进 asyncio.Queue → 立刻返回 task_id
                                                        │
                    后台 Worker 协程 ←───────────────────┘
                          └─ asyncio.to_thread(agent.run)   ← 同步函数丢线程池，不阻塞事件循环
                                └─ 成功/失败/取消 → 回写任务终态 → 清理临时文件
```

### 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/tasks/submit` | 提交任务（multipart：`user_input` + 可选 `delivery`/`return`/`warehouse`） |
| GET | `/api/v1/tasks/{task_id}` | 任务详情 + 完整执行链路（含闸门判定） |
| GET | `/api/v1/tasks/recent?limit=&offset=` | 分页任务列表 |
| POST | `/api/v1/tasks/{task_id}/cancel` | 取消任务 |
| GET | `/health` · `/metrics` | 存活探针（含数据库探测）· 队列与处理量指标 |
| GET | `/docs` | Swagger 文档 |

错误响应统一为 `{"error": {"code": "...", "message": "..."}}`，
code 机器可读（`task_not_found` / `task_not_cancellable` / `unsupported_type` /
`schema_mismatch` / `payload_too_large` / `queue_full` …），方便前端按码分支。

### 上传文件是真的会被用上

三个上传字段按业务口径映射到数据源文件：

| 表单字段 | 角色 | 是否驱动查询 |
|---|---|---|
| `delivery` 发货订单 | 订单明细 | ✅ 本次任务改用这份数据查单/查物流/判库存 |
| `return` 退货订单 | 售后单 | 暂存留痕（当前查询层未消费退货数据） |
| `warehouse` 仓库退货订单 | — | 暂存留痕（数据源无对应文件） |

实现方式是"任务级数据源"：在任务执行的这段窗口内，把全局数据源单例临时换成指向
本次上传文件的新实例，`finally` 里必定还原 —— 不这么做，A 客户上传的订单会串到
B 客户的任务里。因此**执行阶段加锁串行**（`WORKERS>1` 目前只在取任务这一层并行）。

安全上做了四件事：文件名只取最后一段（杜绝路径穿越）、边读边计数超限即中断、
白名单扩展名 + 订单必需列校验（给"缺哪一列"的人话报错）、任务结束即删临时目录。

### 取消是"协作式"的

Python 没法从外部硬杀死一个正在运行的函数。所以取消的语义是：
- **还在排队** → 立刻取消，Worker 取到它会直接跳过；
- **正在执行** → 置起取消标志位，Agent 在下一个检查点（每轮 LLM 调用之前）主动停下。

最坏延迟 = 当前这一步（一次模型调用或一次工具执行）的耗时。接口在 `message` 里
会说明属于哪种情况。任务状态扩展了 `cancelled`。

### 配置

全部走环境变量 / `.env`，见 `.env.example`。优先级：真实环境变量 > `.env` > 代码默认值。

- `MODEL_API_KEY` / `MODEL_NAME`（本项目原名）与 `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL`
  **两套命名都支持**，`src/api/settings.py` 会自动做别名映射。
- `WORKERS`、`MAX_UPLOAD_SIZE`、`MAX_QUEUE_SIZE`、`ALLOWED_UPLOAD_SUFFIXES`、
  `USE_UPLOADED_DATASET`、`AGENT_FORCE_MOCK`、`CORS_ORIGINS` 等。

> 调试技巧：`AGENT_FORCE_MOCK=true` 可以让服务完全不调大模型，离线也能跑通全流程
> （自动化测试就是靠它保证不误调真实 API 的）。

### 容器化（Docker）

```bash
docker compose up --build -d      # 构建并后台启动，http://127.0.0.1:8000/docs
docker compose logs -f api        # 看日志
docker compose stop              # 停止（保留容器）
docker compose down              # 销毁容器（data/ 里的审计库不受影响）

# 端到端验收（41 项：探活 / 异步 / 附件驱动查询 / 取消 / 分页 / 审计落盘 …）
.venv\Scripts\python.exe scripts\acceptance_docker.py
```

要点：

- **审计记录跟着宿主机走。** `docker-compose.yml` 把 `./data` 挂进容器，SQLite 落在宿主机文件上 ——
  所以 `docker compose down` 之后重建容器，历史任务与执行流水依然查得到（已实测验证）。
- **镜像必须单进程跑。** 后台任务队列是进程内的 `asyncio.Queue`，且「上传文件替换数据源」
  依赖 `worker.py` 里的执行锁保证不串台 —— 多副本需换外部队列（Redis）与外部数据库。
- **构建顺序有讲究。** `Dockerfile` 先只 copy `requirements.txt` 再装依赖，最后才 `COPY . .`，
  这样日常改代码不会触发重装 pandas（否则每次多等 3 分钟）。
- **`.dockerignore` 保护敏感/无用文件**：`.venv/`、`data/*.db`、`.env*`、`logs/`、`outputs/`
  都不会进镜像（`data/mock/` 保留，因为容器里跑默认数据源要用）。

## 里程碑（见 docs/architecture.md 与立项方案）

- W1: MVP 跑通（工具层 + 工单接入 + 基本流程）✅
- W2: 安全闸门（敏感动作 + 二次确认 + 参数校验 + 参数名规范化 + 库存闸门）✅
- W3: 记忆 + 评测（画像 + 会话记忆 + 评测集 + 量化报告）✅
- W4: 收口（人工确认控制台 + 审计留痕 + 演示闭环 + 文档）✅
- W5: 加固轮（业务相对约束 + 执行器闭环 + 会话/闸门隔离 + 评测判定重写 + 元测试）✅
- W6: 持久化层（SQLite + SQLAlchemy 审计留痕 + `query_cli` 历史查询）✅
- W7: Web 服务化（FastAPI + asyncio 队列 + 文件上传数据集 + 协作式取消 + Docker 化）✅

## 脚本清单

| 脚本 | 用途 |
|---|---|
| `scripts/run.py` | 交互入口（mock / `--llm` 真实模式） |
| `scripts/run_api.py` | **Web 服务入口**（`--host` / `--port` / `--reload` / `--workers`） |
| `scripts/migrate_csv_to_db.py` | **数据迁移**：CSV → SQLite（`--reset` 重建 / `--dry-run` 预演） |
| `scripts/demo_fts.py` | **RAG 演示**：写入历史案例 + FTS5 关键词检索 + 相似度分数（`--keep` 保留已有案例，`--agent` 跑端到端） |
| `scripts/acceptance_docker.py` | **容器化验收**：对已启动的服务跑 41 项端到端检查（探活/异步/上传驱动/取消/分页/审计落盘） |
| `scripts/approval_console.py` | **人工确认控制台**：查看待确认 → 批准并执行 / 驳回 → 审计导出（`--demo` 自动演示） |
| `scripts/run_evaluation.py` | 真实 LLM 端到端评测（`--fast` 快速模式），产出量化报告到 `outputs/` |
| `scripts/demo_llm.py` | 真实 LLM 全链路演示（查单/物流/库存 + 建议 + 闸门） |
| `scripts/demo_security.py` | 安全加固演示（参数别名 / 业务相对约束 / 库存闸门） |
| `scripts/demo_memory.py` | 记忆演示（画像增量 + 会话与画像双重隔离） |
| `scripts/smoke_test.py` | 冒烟测试（数据源/工具/Agent/LLM 解析器） |
| `scripts/check_llm.py` / `list_models.py` | LLM 连通性 / 平台模型列表 |

