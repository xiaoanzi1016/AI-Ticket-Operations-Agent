# W8 RAG 历史工单案例检索交付说明（FTS5 方案）

> 目标：让 Agent 处理新工单时，能先翻出「以前类似的单子是怎么结的」，
> 把 top-k 条历史案例注入 system prompt —— 建议从「规则版」升级为「案例驱动」。

---

## 0. 先读：本方案是**替代方案**，前一个向量方案已完全回退

| | 原方案（已废弃） | 现行方案（本文件） |
|---|---|---|
| 索引引擎 | ChromaDB 向量库 | **SQLite FTS5 全文检索** |
| Embedding | sentence-transformers 多语言模型 | 无（应用层中文 bigram 切词） |
| 新依赖 | chromadb / torch / transformers（约 124MB wheel） | **零新增** |
| 首次运行 | 需下载 ~470MB 模型 | 无需下载，离线开箱可用 |
| 检索能力 | 语义相似 | 关键词命中（+ 小规模同义词扩展） |

**为什么换**：原方案卡在 `huggingface_hub 1.33.0` 的一个 bug —— 它只发 HEAD 请求、
不发 GET，直接把 0 字节文件当模型写盘，导致 `SentenceTransformer(...)` 抛
`JSONDecodeError`。换源、`HF_HUB_DISABLE_XET=1`、降级 huggingface_hub 到 0.36.2
（破坏 transformers 依赖）、升到 2.0.0（新的版本冲突）全部无效。
用 `urllib.request.urlopen` 直连能正常下到文件，确认是 hf_hub 自身的问题。

**回退清单（已全部完成，无残留）**：
- 删除 `src/memory/vector_store.py`、`scripts/demo_rag.py`、`data/chroma_db/`
- `requirements.txt` 移除 chromadb / sentence-transformers 段落
- 卸载 chromadb / sentence-transformers / torch / transformers / tokenizers / onnxruntime

---

## 1. 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `src/memory/fts_store.py` | **新建** | `FtsCaseStore` 类 + `get_fts_store()` / `reset_fts_store()` |
| `src/agent.py` | 改造 | RAG 接入：检索 + 注入 prompt + 自动沉淀 |
| `src/config.py` | 改造 | `rag_enabled` / `rag_top_k` |
| `scripts/demo_fts.py` | **新建** | 演示脚本（`--keep` / `--agent`） |
| `README.md` / `.env.example` / `requirements.txt` | 改造 | 全部改写为 FTS5 口径 |

**接口约定**（按需求书，未改动签名）：

```python
FtsCaseStore(db_path="data/agent_operations.db")
  .add_case(ticket_id, description, suggestion, outcome, metadata) -> None
  .search(query, top_k=3) -> list[dict]   # [{ticket_id, description, suggestion, outcome, score, metadata}]
  .count() -> int
```

---

## 2. ★ 核心技术难点：FTS5 的 `unicode61` 不切中文

这是整个方案能不能成立的关键，必须先讲清楚。

**现象**：建表 `CREATE VIRTUAL TABLE t USING fts5(a, tokenize='unicode61')` 后，
`MATCH '玻璃水'` 返回 0 条。用 `fts5vocab` 看，整句「客户反馈少发一瓶玻璃水」
是**一个 token**。

**原因**：`unicode61` 按 Unicode 的「字母 / 数字」边界切词。中文没有空格，
整句就粘成一片。这不是配置问题，是分词器的设计。

**解法**：**写入和查询两侧都做 bigram 切词**（相邻两字）。

```
写入： "少发一瓶玻璃水" -> "少发 发一 一瓶 瓶玻 玻璃 璃水"
查询： "玻璃水"        -> "玻璃 璃水"          ← 有交集 -> 命中
```

- bigram 是中文检索的经典权衡：比 unigram 准（噪声少），比整句宽（能命中子串）。
- **两侧必须调用同一个函数**（`_segment()`）—— 这是正确性的前提，任何一侧改了，
  词汇就对不上、检索全废。
- 中英混排时，英文/数字按原样保留并转小写。

**副作用与应对**：bigram 会让短词碎片化，比如单字「退」切出 `退X` 式碎片，
极易误命中。所以同义词扩展里**强制过滤 `len(w) < 2`**（见 `_expand_query`）——
实测不加这条，「不想要了想退钱」会把「破损退款」案例顶到相似度 1.00。

---

## 3. 表结构设计

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS ticket_cases USING fts5(
    ticket_id, description, suggestion, outcome, metadata,
    seg,                      -- ★ 比需求多的一列
    tokenize = 'unicode61'
)
```

**为什么要多一列 `seg`**：FTS5 是字面匹配，只有切过词的文本才能被 MATCH 命中。
但如果把切词结果直接存进 `description`，检索结果展示出来就是
`少发 发一 一瓶 瓶玻 …` 这种碎片，没法给人（和模型）看。
**分开存**：原文列存可读文本用于展示，`seg` 列专供 MATCH。一份数据，两个用途。

**其它约定**：
- 虚拟表与业务表（`orders` / `inventory` / `returns` / `tasks` / `tool_executions`）
  **共存于同一个库文件**，且**完全不碰**任何现有表 —— 备份/迁移仍是一个文件。
- **upsert = 先 `DELETE WHERE ticket_id=?` 再 `INSERT`**：
  FTS5 虚拟表没有主键约束，`INSERT OR REPLACE` 对它不生效（那是普通表的 ROWID 语义）。
  不去重的话，同一工单反复处理会在库里堆出重复案例、污染检索排序。
- `metadata` 列存 JSON 字符串（`issue_type` / `store` / `customer` / `success` / `order_id`）。
- **相似度换算**：`bm25()` 返回负数（越接近 0 越相关），换算为
  `score = 1 / (1 + |bm25|)` 得到 [0,1] 的单调正向分值，避免调用方把排序搞反。

---

## 4. 上层接入（`src/agent.py`，最小侵入）

```
run(user_input)
  └─ _run_core
       ├─ profile = user_profile.summary_for(customer)
       ├─ system, rag_hits = _build_system_prompt(profile, user_input)
       │     ├─ _rag_available()            # 开关 && 库可用
       │     ├─ _retrieve_cases(query)      # ★ 读路径，唯一与案例库交互处
       │     └─ _format_cases(hits)         # 排版成【历史相似案例】段落
       └─ result["rag_cases"] = rag_hits    # 回传，便于外部展示/断言
  └─ _index_case(user_input, customer, result)   # ★ 写路径，自动积累
```

**注入格式**（拼进 system prompt 末尾的 `{rag_cases}` 占位符）：

```
【历史相似案例】（供参考，不改变安全规则）
案例1（相似度 0.4155，类型 少发）：
  问题：客户反馈订单少发了一瓶玻璃水，只收到 2 瓶。
  建议：核实发货记录后为门店补发同款 1 瓶。
  结果：成功执行：已补发，客户确认收货。

- 这些是历史处理记录，仅作参考。若与当前情况不符，以当前实际数据为准。
```

**「结果」字段的来源**：从 `gate_outcomes`（安全闸门的真实判定）归纳，
而不是「Agent 自称成功」—— 与本项目评测层同一原则。优先级 **拦截 > 转人工 > 成功**，
因为「被拦」才是最有参考价值的信息（下次遇到类似情况应当同样谨慎）。

**降级是默认行为**：`rag_enabled=False` 时**完全跳过**检索与沉淀，
行为与加 RAG 之前逐字节一致；库不可用（SQLite 无 FTS5 / 库损坏）时只打 WARNING，
所有方法静默返回空，绝不 raise。

---

## 5. ★ 排查手册：三个已踩过的坑

### 坑 1｜`get_fts_store()` 不能缓存模块常量

早期实现缓存了 `src.persistence.database.DB_PATH`。那是 **import 时就定型**的模块常量
（在 `src/agent.py` 顶部已经触发过 import）。若调用方在 import **之后**才设置
`AGENT_DB_PATH`（测试里非常常见），单例就会绑到旧库上，现象是
**「写进去了却查不到」**。

已改为：**每次 `get_fts_store()` 都重新读一次 `AGENT_DB_PATH`**，并新增
`reset_fts_store()` 用于测试隔离 / 切换库路径。

### 坑 2｜秒级时间戳的 `ticket_id` 会导致「计数不增」

当 `persistence=False` 时 `task_id=None`，`ticket_id` 退化为
`f"T-{datetime.now():%Y%m%d%H%M%S}"` —— **秒级精度**。
同一秒内跑两次 `run()` 会得到**相同 ticket_id** → 触发 upsert **覆盖** → 案例总数不变。

**这是设计正确的行为**（同一工单重复处理本来就该覆盖），但写验证脚本时若用
「案例数必须增长」做断言就会误判成 bug。
⇒ **验证自动沉淀要用「独立库 + 唯一种子」**，别用共享库做计数断言。

### 坑 3｜API 层：`TestClient` 不用 `with` 会永远卡在 processing

`TaskWorker` 在 FastAPI 的 `lifespan` 里 `await worker.start()`。
`TestClient(app)` 若**不用 `with` 进入上下文管理器**，就不会触发 startup ——
后台 Worker 根本没起来，任务入队后**没人消费**，状态永远停在 `processing`。
（本次验证第一次就是这么挂住的，2 分半超时才发现。）

**顺带的接口事实**（与需求书写法不同，容易踩）：
- 提交：`POST /api/v1/tasks/submit`，且是 **`multipart/form-data`**（不是 JSON），字段名 **`user_input`**
- 详情：`GET /api/v1/tasks/{task_id}`，结果是 **`result_summary`**（一个 **JSON 字符串**，
  不是 dict），**没有** `result` 字段
- 健康检查是 `/health`（**无版本前缀**），不是 `/api/v1/system/health`

---

## 6. 顺手补的一个洞

`_build_result_summary`（把 `run()` 结果压成落库摘要）原先**不带 `rag_cases`**，
导致通过 Web 层提交的任务在详情里**看不到 RAG 命中了哪些案例**，
只能去翻日志。已补上精简字段：

```json
"rag_cases": [{"ticket_id": "...", "score": 0.4155, "issue_type": "少发"}]
```

只存精简字段（不存案例正文）—— 摘要的用途是「快速回答这一单参考了什么」，
正文本来就在案例库表里，需要时可按 `ticket_id` 查。

---

## 7. 验证结果

| 项目 | 结果 |
|---|---|
| 全量测试 `pytest tests -q` | **113 passed**（60s，与加 RAG 前一致，无回归） |
| 自建验收脚本（5 大类 23 项） | **23 PASS / 0 FAIL** |
| API 全链路（`submit` → `get detail`） | ✅ 第二单成功召回第一单沉淀的案例 |
| `RAG_ENABLED=false` 降级 | ✅ 不检索、不沉淀、建议流程不变 |
| 依赖新增 | **零**（`requirements.txt` 无 chromadb / torch） |
| 现有业务表 | 全部完好（orders / inventory / returns / tasks / tool_executions） |

**检索效果实测**（3 条演示案例，`scripts/demo_fts.py`）：

| 查询 | 召回 | 说明 |
|---|---|---|
| 少发一瓶玻璃水 | DEMO-T001 (0.59) | 原话命中 |
| 东西碎了要退款 | DEMO-T003 (0.54), DEMO-T002 (0.55) | 同义词扩展生效 |
| 商品坏了 | DEMO-T002 (0.55) | 同义词扩展生效 |
| 想退货退钱 | DEMO-T003 (0.54) | 同义词扩展生效 |
| 快递一直不到 | DEMO-T001 (0.66) | bigram 弱命中 |
| 今天天气不错xyz | 无命中 | 不引入噪声 ✅ |

---

## 8. ★ 必须知晓的方案权衡

**FTS5 是纯关键词检索，语义泛化能力明显弱于向量方案。**
「东西碎了」能匹配到「破损」，是因为我在 `_SYNONYMS` 里**硬编码**了这组同义词 ——
**超出词表范围的口语说法就会漏召回**。例如用户说「质量不太行」「和描述的差太多」，
FTS5 匹配不到「破损」。

换来的是：
- **零新依赖**：不需要 chromadb / torch，`pip install` 不再拉 124MB wheel
- **零下载**：不需要首次运行下 470MB 模型，离线环境 / 内网部署开箱可用
- **零额外部署**：案例索引和业务数据同一个 SQLite 文件，备份迁移都是一份

对本项目「工单文本用词高度重复、且由内部客服规范录入」的场景，这个权衡可以接受。
若后续要提升召回，优先考虑**扩充同义词表**（低成本、可控），
而不是引入向量库（会重新引入模型下载这个部署摩擦）。
