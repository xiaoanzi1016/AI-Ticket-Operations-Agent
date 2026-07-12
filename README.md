# AI-Ticket-Operations-Agent

企业级智能工单/售后运营 Agent，基于 DeepSeek LLM + FastAPI 构建，支持订单查询、物流追踪、库存校验、退款/补发建议生成、历史案例检索（RAG）和退货风险自动升级。

## 技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Python 3.11 |
| AI/LLM | DeepSeek API、OpenAI Function Calling |
| 后端 | FastAPI + Uvicorn |
| Agent | 自研编排层（工具注册、安全闸门、会话记忆） |
| 数据层 | SQLAlchemy 2.x + SQLite（业务数据 + 台账双库） |
| 检索增强 | SQLite FTS5 全文检索（零依赖 RAG） |
| 测试 | pytest 129 passed |
| 部署 | Docker + docker-compose |

## 核心能力

- **AI Agent 编排**：工具调用、安全闸门、会话记忆、可观测性
- **业务工具**：订单查询、物流追踪、库存校验、建议生成、人工升级
- **数据迁库**：CSV → SQLite，双通路降级（DB-first + CSV fallback）
- **RAG 检索**：FTS5 + bigram 切词 + 同义词表，零额外依赖
- **退货风险**：客户退货率自动升级人工、SKU 高风险提示
- **持久化审计**：任务台账、工具流水、闸门判定全落库

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
pytest tests/ -q

# 6. 启动 API 服务
python scripts/run_api.py
```

## 环境变量

复制 `.env.example` 为 `.env`，按需修改：

```bash
# LLM 配置
DEEPSEEK_API_KEY=your_api_key_here

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

tests/                    # 129 项 pytest 测试
docs/                     # 交付文档
```

## 测试

```bash
# 运行全部测试
pytest tests/ -q

# 运行特定测试
pytest tests/test_return_risk.py -v
pytest tests/test_persistence.py -v
```

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

### 双通路数据源设计

`DataSource`（CSV）与 `DatabaseDataSource`（SQLite）保持完全同构，由 `build_data_source()` 工厂函数选路。任何环节失败自动降级 CSV，绝不让"库没建好"变成"服务起不来"。

## 实习背景

本项目为 2026.05 - 2026.07 在上海伍特尔集团运营实习期间独立开发，用于智能化工单处理与售后决策支持。

## License

MIT
