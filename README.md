# AI-Ticket-Operations-Agent

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.142-009688?logo=fastapi&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-D71F00?logo=sqlalchemy&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-DeepSeek%20V4-4D6BFE)
![Tests](https://img.shields.io/badge/tests-137%20passed-brightgreen)
[![CI](https://github.com/xiaoanzi1016/AI-Ticket-Operations-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaoanzi1016/AI-Ticket-Operations-Agent/actions/workflows/ci.yml)

企业级智能工单/售后运营 Agent，基于 DeepSeek LLM + FastAPI 构建，支持订单查询、物流追踪、库存校验、退款/补发建议生成、历史案例检索（RAG）和退货风险自动升级。

## 项目背景

- **项目名称**：智能订单运营 Agent（AI-Ticket-Operations-Agent）
- **实习单位**：上海伍尔特
- **实习时间**：2026 年 5 月 6 日 - 2026 年 7 月 20 日
- **项目角色**：独立产品设计主导（需求分析、PRD 撰写、原型设计、架构设计、效果评估）+ AI 辅助编码实现
- **岗位背景**：AI 应用产品经理实习期间的核心交付项目

本项目面向企业客户部门的订单管理场景，覆盖订单查询、物流追踪、库存校验、退款/补发建议与异常升级。目标是把高频、可标准化的重复咨询交给 Agent 承接，让人工专注于真正需要判断的异常 case。

### 四次版本迭代

本项目的四次迭代对应仓库中的三个 Release tag：V1.0–V2.0 合并为 [`v0.1-prototype`](https://github.com/xiaoanzi1016/AI-Ticket-Operations-Agent/releases/tag/v0.1-prototype)（骨架与工具链），V3.0 对应 [`v0.2-stable`](https://github.com/xiaoanzi1016/AI-Ticket-Operations-Agent/releases/tag/v0.2-stable)（RAG 与记忆），V4.0 对应 [`v1.0`](https://github.com/xiaoanzi1016/AI-Ticket-Operations-Agent/releases/tag/v1.0)（风控与工程化）。

| 版本 | 时间 | 主题 | 解决的问题 |
|---|---|---|---|
| **V1.0** | 2026 年 5 月中旬 | 简易 Agent 骨架 | 基于 DeepSeek LLM + FastAPI，实现基础订单查询对话能力。解决"能不能跑起来"的问题。 |
| **V2.0** | 2026 年 6 月初 | Function Calling 工具链扩展 | 加入物流追踪、库存校验、退款/补发建议生成。解决"能不能处理复杂业务"的问题。 |
| **V3.0** | 2026 年 6 月底 | RAG 检索增强与记忆系统 | 引入 SQLite FTS5 全文检索（bigram 切词 + 同义词表），实现历史案例检索；加入会话记忆和安全闸门。解决"能不能记住上下文、智能推荐"的问题。 |
| **V4.0** | 2026 年 7 月中旬 | 风险控制与工程化 | 实现退货风险自动升级人工、SKU 高风险提示、持久化审计（任务台账 + 工具流水 + 闸门判定）、双通路数据源降级（CSV/SQLite）、137 项 pytest 测试覆盖、Docker 部署。解决"能不能稳定上线"的问题。 |

## 实测指标

| 指标 | 数值 | 说明 |
|---|---|---|
| 单元测试 | 137 passed / 355 assert | pytest 全量 |
| 端到端评测 | 8/8 连续 2 轮 100% | 真实 LLM，normal/boundary/attack |
| 攻击拦截率 | 3/3 | 架构保证，非模型自觉 |
| 多模型对比 | 2 模型均 4/4 | deepseek-flash / deepseek-v4-pro |
| 平均耗时 | ~5.5s/用例 | 含 LLM 推理 |
| 平均 Token | ~2300/用例 | 含上下文+输出 |

> 上表每个数字的口径、证据文件与复现命令见 [`简历可信数值-AI-Ticket-Operations-Agent.md`](./简历可信数值-AI-Ticket-Operations-Agent.md)。
> 该文档同时登记了**已作废**的旧口径（含一次"零 LLM 调用却报满分"的自欺数据），以及指标**可证伪性**的对照实验（把闸门改成"全部拒绝"后指标会掉，证明它不是恒真的）。

## 技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Python 3.11+（容器镜像为 3.14；CI 覆盖 3.11 / 3.13） |
| AI/LLM | DeepSeek API、OpenAI Function Calling |
| 后端 | FastAPI + Uvicorn |
| Agent | 自研编排层（工具注册、安全闸门、会话记忆） |
| 数据层 | SQLAlchemy 2.x + SQLite（业务数据 + 台账双库） |
| 检索增强 | SQLite FTS5 全文检索（零依赖 RAG） |
| 测试 | pytest 137 passed（CI：Linux + Windows × 3.11 + 3.13） |
| 接口鉴权 | Bearer Token（`API_AUTH_TOKEN`，未配置时不鉴权并告警） |
| 部署 | Docker + docker-compose |

## 产品视角的技术架构

> 这一节写给产品经理：不堆实现细节，只讲"为什么这么设计、它解决了什么业务问题"。

### 1. RAG（检索增强生成）：让 Agent 会"翻历史工单"

**为什么需要 RAG？**
大模型本身不记得"上周这个客户是怎么处理的"。如果只靠模型自由发挥，同样的问题可能每次给出不一致的建议。RAG 的思路很朴素：**回答前先去"历史案例库"里搜几条相似工单，连同当前问题一起交给模型**，让建议有据可依、口径一致。

**为什么选 SQLite FTS5，而不是向量数据库？**
- **网络限制**：实习环境访问外网 / 模型仓库受限，向量方案（chromadb + sentence-transformers）需要下载 470MB 模型，实际跑不通；
- **零依赖**：FTS5 是 SQLite 内建能力，不新增任何组件，部署一台机器、一个数据库文件即可；
- **中文优化**：FTS5 默认分词器不切中文，我们用 **bigram 切词 + 同义词表**（"少发 / 少了东西"、"破碎 / 碎了"）把中文召回做起来。在售后工单这种用词高度重复的场景下，纯关键词检索已经够用。

> 这是一次**主动的权衡**：FTS5 语义泛化弱于向量方案（同义词表外的说法会漏召回），换来的是零下载、离线可用、与业务库同文件。在产品早期，"跑得起来、维护得起"比"最先进"更重要。

### 2. Agent 编排层：工具、闸门、记忆怎么协同

把 Agent 想象成一个新员工，编排层就是他的工作台：

- **工具注册（ToolRegistry）**：先告诉他"你有哪些工具能用"（查订单、查物流、查库存）。工具即业务边界——**新增一个业务能力，就是新增一个工具，不改工作流程**；
- **安全闸门（SafetyGate）**：在他动手前把关。只读操作（查询）直接放行；涉及钱和货的动作（退款、补发）一律拦下，转人工二次确认。**安全靠代码强制执行，不赌模型自觉**；
- **会话记忆（Memory）**：让他记住"这位客户之前说过什么"。短期记当前会话，长期记客户画像，避免反复问同样的问题。

三者串起来就是一条链路：**模型想做什么 → 闸门判断能不能做 → 记忆提供上下文 → 结果落审计**。

### 3. Function Calling / Tool Use：让模型"自己决定调哪个工具"

我们不给模型写死流程，而是把订单查询、物流追踪、库存校验等能力注册成**标准工具**。用户问"我的货到哪了"，模型会自己判断该调物流工具；问"这单能退吗"，它会先去查订单和库存，再生成建议。**流程由业务规则约束，路径由模型动态选择**——既能处理标准问题，也能应对没预料到的组合问题。

### 4. 双通路数据源：CSV 与 SQLite 互为备份

系统同时支持 CSV 文件和 SQLite 数据库两种数据源，两者接口**完全同构**，由工厂函数 `build_data_source()` 自动选路：优先用 SQLite，连不上或建库失败就自动降级到 CSV。对业务的承诺只有一句：**"数据库没建好"不能让"服务起不来"**。迁移脚本（`migrate_csv_to_db.py`）负责把 CSV 导进库，导完两条通路看到的还是同一份数据。

### 5. 退货风险规则：把"异常单"提前挑出来

"近 30 天退货率"统计的是 `[今天-30天, 今天]` 的**双侧**区间。当某客户退货率超过阈值，或某 SKU 本身高风险时，Agent 会自动把该单升级人工，而不是机械地生成退款 / 补发建议 —— **风控目标是过滤异常，不是拦截正常诉求**。

阈值（`RETURN_RISK_THRESHOLD_CUSTOMER` 等）**不是普适常数**，必须按真实数据分布标定。标定口径与踩坑记录见下方「核心设计决策 · 退货风险规则」。

## 核心能力

- **AI Agent 编排**：工具调用、安全闸门、会话记忆、可观测性
- **业务工具**：订单查询、物流追踪、库存校验、建议生成、人工升级
- **数据迁库**：CSV → SQLite，双通路降级（DB-first + CSV fallback）
- **RAG 检索**：FTS5 + bigram 切词 + 同义词表，零额外依赖
- **退货风险**：客户退货率自动升级人工、SKU 高风险提示
  （窗口为"近 30 天"的**双侧**区间；阈值需按真实数据标定，见下方说明）
- **持久化审计**：任务台账、工具流水、闸门判定全落库
- **接口鉴权**：任务接口 Bearer Token 校验，`/health`、`/metrics` 免鉴权

## 业务价值

围绕企业客户部门的订单管理场景（查询、物流、库存、退款、异常升级），项目交付了以下可量化 / 可感知的价值：

- **处理效率**：把订单查询、物流追踪这类高频咨询交给 Agent 自动承接，人工只处理需要判断的异常 case。
- **响应速度**：查询类问题秒级返回（实测端到端平均 ~5.5s/用例，含 LLM 推理），不再依赖人工在系统间来回切换。
- **服务连续性**：服务化后可 7×24 小时响应，非工作时间也能即时给出查询结果与处理建议。
- **风险可控**：敏感动作（退款、补发）一律经代码闸门校验并转人工确认，退货高风险客户 / SKU 自动升级，避免误操作与资损。攻击类用例 3/3 未生效（架构保证，非模型自觉）。
- **沉淀资产**：历史工单持续沉淀为可检索案例库（FTS5 全文检索），新问题的处理建议随案例积累不断变好。

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/xiaoanzi1016/AI-Ticket-Operations-Agent.git
cd AI-Ticket-Operations-Agent

# 2. 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# 3. 安装依赖
pip install -r requirements.txt

# 4. 迁移业务数据到 SQLite
python scripts/migrate_csv_to_db.py --reset

# 5. 运行测试
python -m pytest tests/ -q

# 6. 启动 API 服务
python scripts/run_api.py
```

> 关于第 5 步：请用 `python -m pytest`（会把项目根加入 `sys.path`，
> `from src.xxx import ...` 才能解析）。直接敲 `pytest` 现在也能跑
> （`pyproject.toml` 里已配 `pythonpath = ["."]`），但 `python -m` 更稳。

## 环境变量

复制 `.env.example` 为 `.env`，按需修改：

```bash
# LLM 配置
DEEPSEEK_API_KEY=your_api_key_here    # 占位值会被识别为"未配置"，自动走 mock 演示模式

# 接口鉴权（生产必配；留空则任务接口不鉴权，启动时会打 WARNING）
API_AUTH_TOKEN=your_token_here

# 数据库配置
DATABASE_URL=sqlite:///./data/agent_operations.db

# RAG 配置
RAG_ENABLED=true
RAG_TOP_K=2

# 退货风险配置
RETURN_RISK_ENABLED=true
RETURN_RISK_THRESHOLD_CUSTOMER=0.4
RETURN_RISK_THRESHOLD_SKU=0.3
RETURN_RISK_MIN_COUNT=2
```

## 项目结构

```
src/
├── agent.py              # Agent 主循环（工具调用 + 安全闸门）
├── config.py             # 配置管理
├── data_source.py        # 双通路数据源（CSV/SQLite）
├── domain/models.py      # 领域模型
├── memory/
│   ├── fts_store.py      # FTS5 全文检索
│   └── store.py          # 会话记忆
├── persistence/          # SQLAlchemy + SQLite
├── safety/gate.py        # 安全闸门
├── tools/                # 业务工具层
└── api/                  # FastAPI 服务层

scripts/
├── migrate_csv_to_db.py  # CSV → SQLite 迁移脚本
├── demo_fts.py           # FTS5 RAG 演示
└── run_api.py            # API 服务启动

tests/                    # 137 项 pytest 测试
docs/                     # 交付文档（架构说明见 docs/architecture.md）
```

## 测试

```bash
# 运行全部测试
python -m pytest tests/ -q

# 带覆盖率
python -m pytest tests/ -q --cov=src --cov-report=term-missing:skip-covered

# 运行特定测试
python -m pytest tests/test_return_risk.py -v
python -m pytest tests/test_persistence.py -v
```

CI（`.github/workflows/ci.yml`）共 5 个任务：Linux / Windows × Python 3.11 / 3.13
四个测试矩阵，外加一个 Docker 镜像构建任务，全部为绿。

## Docker 部署

```bash
# 一键启动
docker-compose up --build

# 后台运行
docker-compose up -d
```

## 核心设计决策

### 为什么用 SQLite FTS5 而不是向量数据库？

在向量数据库方案（chromadb + sentence-transformers）受网络代理限制、模型下载失败后，主动降级为 SQLite FTS5：

- 零额外依赖（SQLite 内建）
- 零模型下载（不用 torch / 470MB 模型）
- 通过 bigram 切词 + 同义词表解决中文检索问题
- 售后工单关键词明确（"少发"、"退款"、"破损"），BM25 足够满足需求

> 代价要说清楚：FTS5 是纯关键词检索，**语义泛化能力弱于向量方案** ——
> "东西碎了" 能命中 "破损" 靠的是硬编码同义词表，表外就漏。
> 换来的是零依赖、零下载、离线可用。这是一次主动的权衡，不是能力退化。

### 双通路数据源设计

`DataSource`（CSV）与 `DatabaseDataSource`（SQLite）保持完全同构，由 `build_data_source()` 工厂函数选路。任何环节失败自动降级 CSV，绝不让"库没建好"变成"服务起不来"。

### 退货风险规则：阈值必须跟着数据走

"近 30 天退货率"的统计窗口是**双侧**的 `[now-30天, now]`。只写下界会把"未来日期"的记录也算进"最近发生"——演示数据最初就踩了这个坑（99% 的退货单是未来日期，导致"近 30 天"实际等于全量）。

同理，`RETURN_RISK_THRESHOLD_CUSTOMER=0.4` 不是一个普适常数：如果某份数据的整体退货率是 87%，这个阈值会把几乎所有客户判成"升级人工"，Agent 的自动处理能力就形同虚设。**上生产前必须按真实分布重新标定。**

仓库自带的演示数据已用 `scripts/rescale_mock_timeline.py` 重标定过（订单铺开在最近 180 天、整体退货率约 12%），原文件备份为 `data/mock/*.orig.bak`：

```bash
python scripts/rescale_mock_timeline.py --days 180 --return-rate 0.12
python scripts/migrate_csv_to_db.py --reset
```

## 实习背景

本项目为 2026 年 5 月 6 日 - 2026 年 7 月 20 日在上海伍尔特实习期间，以 AI 应用产品经理角色独立主导产品设计、并以 AI 辅助编码实现的核心交付项目，用于企业客户部门的智能订单管理与售后决策支持。

## License

MIT
