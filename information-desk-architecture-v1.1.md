# information-desk 架构与功能设计文档

> **版本**：V1.1（V1.0 信息源治理版的工程修订）  
> **状态**：设计基线 / 待按验收清单实现  
> **修订日期**：2026-11-13  
> **目标读者**：Python 开发者、MCP 工具开发者、科研情报工作流维护者  
> **部署定位**：单机、本地优先、可定时运行、可由多个 MCP 客户端查询  
> **文档性质**：设计规范；其中代码、SQL、JSON 是拟实施契约，不代表已编码或运行通过。

## 0. 执行摘要与关键决策

`information-desk` 是面向科研与开源技术信息的**源头治理服务**：自动发现 arXiv 论文、Hugging Face Daily Papers/Models、GitHub 仓库，将来源映射到可追溯的本地技术实体，完成保守跨源归并、幂等存储、差量事件生成、基础源质量探查、人工归档和检索，并通过本地 CLI 与 MCP 工具供外部智能体使用。

本版本遵循以下不可妥协的工程原则：

1. **采集即持久化**：成功发现的条目自动写入 `inbox`，不依赖调用方额外执行 `save_item` 才能去重；`save_item` 主要用于人工整理和状态变更。
2. **跨源合并保守进行**：区分“同一技术实体”“官方实现/模型”“仅提及或引用”；只有明确的强证据且无冲突时自动归并。
3. **更新与首次发现分离**：旧项目新增代码、权重或发生重要内容变化，仍必须产生可查询的更新事件。
4. **持久化水位线和完整性标记**：分页未完成、限流、响应不完整或采集错误不得推进成功水位线。
5. **人工数据优先**：采集更新不能无条件覆盖人工编辑的标题、摘要、笔记和审核状态。
6. **单机轻量、协议解耦**：Python + `httpx` + Pydantic + SQLite；核心业务不依赖 MCP、客户端、定时器或特定 Agent。
7. **不夸大来源质量**：根目录发现代码只表示 `detected`，不表示“可运行”；平台 API 元数据不能证明论文真实性或代码正确性。

**V1.1 必做**：3 类平台、4 个采集子源、增量采集、归并/冲突、事件、基本人工审核、条件/全文检索、`inspect_source`、MCP、CLI、离线测试、故障恢复。  
**V1.1 不做**：向量数据库、LLM 自动评审、自动生成日报、消息推送、网页 UI、重型爬虫、自动 clone/安装/运行仓库、模型权重下载、设备兼容性评估、评分排行、长周期 Star 历史分析、多机分布式协调。

> V1.1 输出的是可被日报系统消费的可靠候选源和事件；日报的筛选、总结、推送属于调用方或后续版本。

---

## 1. 需求、边界与非目标

### 1.1 用户故事

- **发现**：查询最近 1–14 天与 Agent、VLM、VLA、具身智能、策略学习、推理优化等主题相关的论文、模型和仓库。
- **消重**：同一论文分别来自 arXiv 与 HF Papers 时只建立一个技术实体；论文与官方 GitHub 仓库在证据充分时关联到同一个实体。
- **追踪**：今天只有论文，之后出现官方实现或模型时，收到 `source_attached` 或 `code_detected` 事件。
- **归档**：人工将实体标记为保存或忽略、添加分类和笔记；下次同步不会重建重复项，也不会覆盖人工决策。
- **检索**：按关键词、主题、审核状态、是否关注、是否检测到代码及时间范围快速查询。
- **运维**：断网、超限、进程崩溃之后可安全重试；可判断某来源究竟是否完整采集。
- **集成**：无客户端时 CLI 可独立运作；有 MCP 客户端时可通过工具获取内容。

### 1.2 质量属性与约束

| 维度 | 约束或目标 |
|---|---|
| 正确性 | 源的稳定身份唯一；重复执行不重复产生实体/相同事件；人工数据不意外覆盖 |
| 完整性 | 不因固定的“返回 30 条”截断采集；不完整扫描有显式 `partial` 状态 |
| 可追溯性 | 每个来源可反查 URL、外部 ID、原始关键字段和关联证据 |
| 可恢复性 | 同一窗口可整段重跑；仅在该窗口完整成功后更新水位线 |
| 安全性 | Token 不进数据库/日志；外部内容视为不可信；仅访问白名单域名 |
| 可维护性 | 适配器、归并、存储和工具层可分别单测；数据库有版本迁移 |
| 部署 | 无 Redis、PostgreSQL、Docker 必需依赖；默认单机单写入者 |
| 搜索规模 | 以数万到十万级实体为设计范围；性能须基于真实样本测试，不承诺未测试 SLA |

### 1.3 对象定义

- **Source（来源）**：某平台的具体论文、仓库或模型，具有稳定可识别身份，例如一篇 arXiv、一个 GitHub repo。
- **Item（实体）**：系统内部对一项独立研究工作/技术项目的表示；允许关联多个 Source。
- **Link（来源关系）**：一个来源对另一个来源的引用、实现、模型发布等关系，附证据及置信级别。
- **Event（变化事件）**：首次发现、新关联来源、重大内容变化或代码检测等可供外部消费的差量记录。
- **Sync Job（采集作业）**：由平台、查询条件、时间窗口构成的可恢复同步单元。

**一个 Item 不应被假定对应一个 GitHub 仓库。** 同一研究可能有多个官方代码仓库；一个框架仓库也可能同时被多篇论文引用。归并始终以“研究实体身份”为中心。

---

## 2. 技术栈与版本策略

| 组件 | 选择 | 说明 |
|---|---|---|
| 运行时 | Python 3.11+ | 兼容常见 Linux 环境 |
| 包管理 | `uv` + `pyproject.toml` + `uv.lock` | 提交锁文件，锁定可重现环境 |
| HTTP | `httpx` | 单一异步客户端；单域名速率/并发限制；显式超时 |
| 数据契约 | `pydantic` v2 | 输入、配置、统一领域记录、输出模型 |
| 持久化 | 标准库 `sqlite3` | WAL、事务、外键、FTS5（若编译支持） |
| XML | `xml.etree.ElementTree` | 解析 arXiv Atom；禁用外部网络文档加载 |
| MCP | **官方 MCP Python SDK v2 的 `MCPServer`** | 禁止混用 v1 `FastMCP` 示例 |
| CLI | `argparse` | 无需额外 CLI 框架 |
| 测试 | `pytest`、`pytest-asyncio`（开发依赖） | Fake Transport / Mock HTTP + Fixture |
| 观测 | 标准库 `logging` | 结构化字段、轮转文件、stderr 不污染 stdio |

SDK v2 的示例导入：

```python
from mcp.server import MCPServer

mcp = MCPServer("information-desk", version="1.1.0")
```

规范性依赖范围：`mcp>=2,<3`、`pydantic>=2,<3`；真正开发时应先针对目标主机与 MCP 客户端完成兼容性测试，再在 `uv.lock` 锁定**具体可用版本**。不要未经验证即手工假定某个补丁版本兼容。MCP 官方 v2 兼容较旧协议客户端，但宿主具体兼容性仍须实测。

---

## 3. 总体架构与模块职责

```text
┌────────────────────────────── 外部调用层 ──────────────────────────────┐
│  systemd timer / cron     本地 CLI      ChatGPT/其他 MCP 宿主/Agent     │
└────────────┬───────────────┬──────────────────┬──────────────────────┘
             │               │                  │
             └───────────────┼──────────────────┘
                             ▼
┌───────────────────── Interface Adapters ─────────────────────────────┐
│  cli.py                   server.py  (MCPServer stdio)               │
└──────────────────────────────┬───────────────────────────────────────┘
                               ▼
┌───────────────────── Application Service ────────────────────────────┐
│  sync()  inspect()  triage()  search()  list_events()  get_status()    │
└──────────────────────────────┬───────────────────────────────────────┘
                ┌──────────────┼───────────────┐
                ▼              ▼               ▼
┌────────────────────┐ ┌────────────────┐ ┌────────────────────────────┐
│ Sync Engine        │ │ Identity       │ │ Persistence                │
│ window / cursor    │ │ Resolver       │ │ SQLite repository          │
│ paging / retries   │ │ evidence       │ │ items / sources / links    │
│ rate limit         │ │ conflicts      │ │ sync_state / events / runs  │
└──────────┬─────────┘ └────────────────┘ └────────────────────────────┘
           ▼
┌────────────────────────── Source Adapters ───────────────────────────┐
│ arXiv API  │ HF Daily Papers │ HF Models │ GitHub Search/Repo API    │
└──────────────────────────────────────────────────────────────────────┘
```

### 3.1 推荐代码结构

```text
information-desk/
├── pyproject.toml
├── uv.lock
├── README.md
├── LICENSE
├── config.example.toml
├── docs/
│   ├── architecture.md
│   └── operations.md
├── src/info_desk/
│   ├── __init__.py
│   ├── config.py
│   ├── domain.py             # Item/Source/Link/Event/SyncResult 定义
│   ├── exceptions.py
│   ├── service.py            # 用例编排，不感知 MCP
│   ├── resolver.py           # 归一化、关系分类、归并与冲突
│   ├── sync_engine.py        # 时间窗口/分页/水位线/增量流程
│   ├── inspect.py            # 轻量内容质量探查
│   ├── http_client.py        # 限速、重试、响应限制
│   ├── sources/
│   │   ├── base.py
│   │   ├── arxiv.py
│   │   ├── hf_papers.py
│   │   ├── hf_models.py
│   │   └── github.py
│   ├── storage/
│   │   ├── sqlite.py
│   │   ├── repository.py
│   │   ├── migrations.py
│   │   └── migrations/001_initial.sql
│   ├── server.py             # MCP Tools 薄适配
│   └── cli.py                # CLI 薄适配
├── tests/
│   ├── fixtures/
│   ├── unit/
│   ├── integration/
│   └── contract/
└── deploy/
    ├── information-desk.service
    └── information-desk.timer
```

`http_client.py` 只能处理通用网络问题，不能解析某平台的业务 JSON；`resolver.py` 不直接访问远端 API；`storage` 不存放 MCP 参数。避免将所有逻辑长期堆在原 `sources.py` 中。

---

## 4. 领域模型与不变量

### 4.1 数据源分类

采集作业 `feed` 与领域来源类型 `source_type` 应分开：

| feed（用于调度） | source_type（用于唯一身份） | source_uid（稳定身份） | external_id（可读定位符） |
|---|---|---|---|
| `arxiv` | `arxiv` | 去版本的 arXiv ID | 例：`2611.01234` |
| `hf_papers` | `hf_paper` | 其对应 arXiv ID | 同上 |
| `hf_models` | `hf_model` | 已验证的模型仓库 ID；若无独立稳定 ID，规范化 namespace/name | 例：`org/model-name` |
| `github` | `github_repo` | GitHub API 的数字 `id` | 例：`owner/repo` |

`github_repo` 的稳定身份必须优先采用 API 数字 `id`，不能单独依赖 `owner/repo`；仓库重命名时更新 `external_id`，但保持来源数据库主键与 `source_uid` 不变。若 HF 模型未提供可证明跨重命名稳定的 ID，则重命名识别作为有限能力，**不能臆测已解决**。

### 4.2 Pydantic 契约（示意）

```python
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

Feed = Literal["arxiv", "hf_papers", "hf_models", "github"]
SourceType = Literal["arxiv", "hf_paper", "hf_model", "github_repo"]
TriageStatus = Literal["inbox", "saved", "ignored"]
CodeStatus = Literal["unknown", "not_detected", "detected"]
EventType = Literal["discovered", "source_attached", "content_changed", "code_detected"]

class SourceRef(BaseModel):
    source_type: SourceType
    source_uid: str
    external_id: str
    url: str

class CandidateSource(BaseModel):
    source: SourceRef
    title: str
    description: str = ""
    published_at: datetime | None = None
    source_updated_at: datetime | None = None
    metadata: dict = Field(default_factory=dict)
    extracted_links: list[dict] = Field(default_factory=list)

class ItemView(BaseModel):
    id: int
    title: str
    summary: str
    category: str
    triage_status: TriageStatus
    is_watched: bool
    code_status: CodeStatus
    notes: str
    sources: list[SourceRef]
    first_seen_at: datetime
    updated_at: datetime
```

### 4.3 系统不变量

- `UNIQUE(source_type, source_uid)`：相同外部身份在 DB 中只能出现一条 Source。
- 每个 Source 恰属于一个 Item；Item 可有多个 Source。
- 只有没有相互冲突的强证据，才可以自动将不同来源挂到已有 Item。
- 多个不同 Item 同时被匹配时不能 `REPLACE`/静默改写归属，必须形成持久化冲突记录。
- 自动同步不能修改 `triage_status`、`notes` 和人工锁定的内容字段。
- `ignored` 不再产生普通用户通知，但可更新最小来源元数据以维持去重；可人工恢复审核状态。
- `is_watched` 与 `triage_status` 独立；已保存的实体仍可以持续追踪。
- 任何已提交的事件具有稳定递增 ID；消费者按 Event ID 读取，不依赖同步调用的临时返回值。
- 时间统一使用 UTC RFC 3339 字符串（例如 `2026-11-13T15:00:00Z`）；显示层负责时区转换。

---

## 5. SQLite 数据库设计

### 5.1 初始化

每条 SQLite 连接均设置：

```sql
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA journal_mode = WAL;
```

`journal_mode` 初始化/数据库维护时设置一次即可；`foreign_keys` 和 `busy_timeout` 对每条新连接生效。写入采用短事务（`BEGIN IMMEDIATE`），不要在数据库事务内等待 HTTP 响应。数据库表使用应用层生成的 UTC 时间串；统一由迁移脚本管理 schema，不在应用启动时任意覆盖历史表。

### 5.2 V1.1 物理 Schema

以下为可作为 `001_initial.sql` 基础的 DDL；字段顺序并非 Python 模型序列化顺序。

```sql
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT 'general',
    triage_status   TEXT NOT NULL DEFAULT 'inbox'
                    CHECK (triage_status IN ('inbox', 'saved', 'ignored')),
    is_watched      INTEGER NOT NULL DEFAULT 0 CHECK (is_watched IN (0, 1)),
    code_status     TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (code_status IN ('unknown', 'not_detected', 'detected')),
    notes           TEXT NOT NULL DEFAULT '',
    title_locked    INTEGER NOT NULL DEFAULT 0 CHECK (title_locked IN (0, 1)),
    summary_locked  INTEGER NOT NULL DEFAULT 0 CHECK (summary_locked IN (0, 1)),
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sources (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id            INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    source_type        TEXT NOT NULL
                       CHECK (source_type IN ('arxiv','hf_paper','hf_model','github_repo')),
    source_uid         TEXT NOT NULL,
    external_id        TEXT NOT NULL,
    url                TEXT NOT NULL,
    title              TEXT NOT NULL DEFAULT '',
    description        TEXT NOT NULL DEFAULT '',
    published_at       TEXT,
    source_updated_at  TEXT,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    last_checked_at    TEXT,
    content_hash       TEXT NOT NULL DEFAULT '',
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source_type, source_uid),
    UNIQUE (source_type, external_id)
);

CREATE TABLE IF NOT EXISTS source_links (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    from_source_id      INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    target_source_type  TEXT NOT NULL,
    target_external_id  TEXT NOT NULL,
    target_url          TEXT NOT NULL DEFAULT '',
    relation_type       TEXT NOT NULL
                        CHECK (relation_type IN
                          ('same_paper','official_code','official_model','references','uses','related')),
    evidence_level      TEXT NOT NULL CHECK (evidence_level IN ('strong','supporting','weak')),
    evidence_origin     TEXT NOT NULL,
    evidence_excerpt    TEXT NOT NULL DEFAULT '',
    observed_at         TEXT NOT NULL,
    UNIQUE (from_source_id, target_source_type, target_external_id,
            relation_type, evidence_origin)
);

CREATE TABLE IF NOT EXISTS sync_state (
    job_key              TEXT PRIMARY KEY,
    feed                 TEXT NOT NULL,
    query_json           TEXT NOT NULL,
    query_version        INTEGER NOT NULL DEFAULT 1,
    completed_until      TEXT,                -- 只有窗口完整扫描才推进
    last_success_at      TEXT,
    last_attempt_at      TEXT,
    last_status          TEXT NOT NULL DEFAULT 'never'
                         CHECK (last_status IN ('never','success','partial','failed')),
    last_error           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS change_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    source_id       INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    event_type      TEXT NOT NULL CHECK (event_type IN
                    ('discovered','source_attached','content_changed','code_detected')),
    event_key       TEXT NOT NULL UNIQUE,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merge_conflicts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    left_item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    right_item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    evidence_json    TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open','resolved','dismissed')),
    created_at       TEXT NOT NULL,
    resolved_at      TEXT,
    CHECK (left_item_id < right_item_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_open_merge_pair
    ON merge_conflicts(left_item_id, right_item_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS sync_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key           TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    status            TEXT NOT NULL CHECK (status IN ('running','success','partial','failed')),
    fetched_count     INTEGER NOT NULL DEFAULT 0,
    new_sources       INTEGER NOT NULL DEFAULT 0,
    new_items         INTEGER NOT NULL DEFAULT 0,
    new_events        INTEGER NOT NULL DEFAULT 0,
    conflict_count    INTEGER NOT NULL DEFAULT 0,
    warning_json      TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_sources_item ON sources(item_id);
CREATE INDEX IF NOT EXISTS idx_sources_recent ON sources(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_triage ON items(triage_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_watch ON items(is_watched, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_seen ON items(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_item ON change_events(item_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON change_events(event_type, id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_job ON sync_runs(job_key, id DESC);
```

**约束说明**：`source_links.target_external_id` 是发现时的规范化定位符，可能尚无匹配的 `sources` 行，因此不能强制外键；重命名仓库时需在归并器中通过平台稳定 ID 重新解析并更新关系。`merge_conflicts` 记录的是**待处理冲突**，不是两个 Item 的合并事实。

### 5.3 检索策略

启用 SQLite FTS5（若运行环境支持）；索引 `items.title`、`items.summary`、`items.notes`。首次建索引后重建索引，并通过触发器或 Repository 原子同步维护。FTS5 不支持时退化到参数化 `LIKE`；用户输入不得直接拼接 SQL，也要限制 FTS 查询长度与语法复杂度。V1.1 不要求中文语义分词，中文搜索按实际 FTS tokenizer 能力验收；需要复杂中文分词时再引入扩展。

### 5.4 事务与数据所有权

- **自动字段**：Source 的标题、简介、源时间戳、哈希、元数据、`last_seen_at`、关系证据、事件。
- **人工字段**：`triage_status`、`is_watched`、`notes`、锁定后的 `title`/`summary`；自动采集无权覆盖。
- **可自动填充的展示字段**：未锁定的 Item 标题/摘要，可按来源质量规则更新；更新应确定性、可复现、记录变更事件。不能把未经人工验证的摘要称为事实核查。
- **合并冲突**：出现多个不同已持久化 `item_id` 时，中止跨实体合并并记录冲突；不得使用 `INSERT OR REPLACE` 重新指派来源。

### 5.5 数据库备份、迁移与清理

- 通过 `PRAGMA user_version` 或独立迁移记录维护 Schema 版本；升级前先备份。
- 备份运行中的 WAL 数据库使用 `sqlite3.Connection.backup()`，禁止仅裸拷贝 `.db` 而忽略 WAL 文件。
- 单次迁移为事务操作；迁移失败回滚。首次从旧 V1 草案数据升级时先生成检查报告（见第 15 章）。
- 不允许默认删除已有 Item 或 `ignored` Source；事件保留期限可配置，默认 V1.1 不主动清理事件。

---

## 6. 标识规范化与跨源归并

### 6.1 标识提取规则

| 来源 | 输入 | 归一化 | 主要防误识别条件 |
|---|---|---|---|
| arXiv | `arxiv:2611.01234v2`、`/abs/2611.01234`、`/pdf/2611.01234.pdf` | `2611.01234` | 去版本、去 `.pdf`；校验年月、位数和 legacy ID 类型 |
| GitHub | `github.com/Owner/Repo.git/tree/main` | 查询 GitHub API 得稳定数字 `id`；`external_id=owner/repo` | 排除非仓库路径；兼容 repo 重命名；忽略 issue/PR 链接作为实体目标 |
| HF Papers | `huggingface.co/papers/2611.01234` | `hf_paper:2611.01234` | 保持为 HF paper Source；与 arXiv Source 通过同论文证据合并 |
| HF Models | `huggingface.co/org/model` | `hf_model:org/model` | 不把 `/datasets/`、`/spaces/`、`/papers/` 当 Model；保护 namespace |

GitHub repo **不能直接全文转小写后当永恒主键**；可读路径可做规范化匹配，长期身份应以 API 数字 `id` 为准。arXiv 旧式标识（如 `hep-th/9901001`）可被识别但不要求 V1.1 扩大采集范围；解析器应避免将其误判成 GitHub 路径。

### 6.2 证据等级与归并决策

| 证据 | 等级 | 默认动作 |
|---|---|---|
| arXiv Source 与 HF Paper Source 使用相同合法 arXiv ID | `strong` | 自动归并（若不存在不同 Item 冲突） |
| 论文页面明确标注官方代码仓库，且仓库 README/项目元数据反向指向**相同论文** | `strong` | 自动归并（仍须检查一对多及已有 Item 冲突） |
| HF Papers 显式 `githubRepo` 指向仓库，且仓库反向明确引用对应论文 | `strong` | 自动归并（同上） |
| 论文元数据仅单向提及 GitHub 仓库、HF Model Card 声称基于某论文 | `supporting` | 保留 Link，待确认或更多证据 |
| README 的引用列表、依赖说明、benchmark 比较中出现论文/仓库链接 | `weak` | 只建关系，禁止自动归并 |
| 标题相似、关键词相同、仓库名近似 | `weak` | 仅作为候选线索，禁止自动归并 |

**重要例外**：一个官方大仓库可能实现多篇论文。即便有双向引用，如果该仓库已绑定另一明确不同的论文实体，不允许通过连通分量将全部实体串联合并；进入冲突队列。

### 6.3 归并算法（确定性）

```text
for each normalized source record:
    A. 用 (source_type, source_uid) 查询已有 Source
       - 命中：固定使用其 existing item_id，更新 Source 元数据
       - 未命中：收集强证据与已存 Source 的匹配结果
    B. target_item_ids = 不重复的强证据命中 Item 集合
       - 0 个：新建 Item + Source，默认 triage_status='inbox'
       - 1 个：Source 附加到该 Item
       - >1 个：不创建第三个临时 Item。将待入库 Source 的规范化记录及候选 Item ID 持久化到待处理队列，并为冲突 Item 对生成 merge_conflicts；人工裁决后再挂接。
    C. 将 source_links 及来源快照在短事务中 upsert
    D. 对比“旧快照 -> 新快照”；生成去重的变更事件
    E. 若为已有 Source，任何跨 Item 改写必须走显式合并流程
```

步骤 B 遇到多 Item 时，**不得任意选一个作为主实体**。即使这会造成暂时性重复，也比隐式丢数据安全。

### 6.4 显式合并与回滚策略

V1.1 默认只通过管理 CLI 执行人工合并（不向所有 MCP 客户端开放破坏性合并工具）：

```bash
uv run info-desk conflicts list
uv run info-desk conflicts resolve <conflict_id> --winner <item_id>
uv run info-desk conflicts dismiss <conflict_id>
```

管理员合并时：

1. 校验冲突未处理、源和目标仍存在、没有循环或其他未解决归属矛盾。
2. 在同一个 SQLite 事务中将 Source 归属移到胜出 Item；保留所有来源、证据。
3. 对两个 Item 的人工笔记、标题锁定、审核状态和关注标志采取**显式合并规则**：笔记不自动覆盖，必要时生成待人工处理内容；未确认合并时应拒绝。
4. 更新相关事件引用与 FTS 索引；将冲突标为 `resolved`；写入可审计的操作记录（V1.1 可存在 `sync_runs`/专用结构化审计日志，后续版本可独立 `audit_log` 表）。
5. 删除失去 Source 的空实体必须经明确确认，默认保留为待清理；数据库事务可回滚，但**提交后的人工合并不承诺一键自动撤销**，需要备份恢复或反向编辑。

---

## 7. 增量采集、时间窗口与事件

### 7.1 为什么不能用 `days=1 + limit=30` 当作采集进度

查询最近一天只定义**观察范围**，不能证明当天所有记录已被枚举；API 分页、索引延迟、搜索排序变化与 GitHub 搜索结果上限都会造成漏抓。`fetch_updates.limit` 只能限制**返回体**，不能限制扫描完整度。

### 7.2 Sync Job 唯一键与水位线

- `job_key = sha256(feed + canonical_query_json + query_version)`：同一作业有自己的进度。
- `completed_until`：对指定查询，**已经完整成功扫描到的 UTC 时间上界**，与运行时间不同。
- `last_success_at`：最近一次成功执行该查询的时间，用于运维展示。
- `overlap_hours`：每次成功后重扫一段重叠窗口（默认建议 48 小时，可配置），抵御 API 索引延迟与临界时间错过。
- `settle_lag_minutes`：采集上界相对当前时间后移的安全滞后，取值按平台实际特性配置；无充分证据时设为 0 并依赖重叠扫描。

首次启动可选择 `--days 2` 初始化；之后若存在 `completed_until`，由 Sync Engine 决定窗口。手动的历史补抓与周期性自动同步使用不同 `job_key`，避免人工回溯意外推进周期任务的水位线。

### 7.3 单个作业的执行过程

```text
1. 获取该 job_key 的独占同步锁（一个作业禁止并发同步）
2. 读取 completed_until，构造 [start,end) 时间窗口
3. 将大窗口切为有界小段（GitHub 优先按日期切片）
4. 逐段分页采集；每页规范化、归并并事务入库
5. 将每页 Source 的重要变化转换成持久化 Event
6. 成功完成某时间段全部页后，才推进该段的 completed_until
7. 若有 429/403、timeout、incomplete_results、API 上限、max_pages 命中：
   - 该段 status='partial' 或 'failed'
   - 保留以前成功的 completed_until；当前段不推进
   - 下次重复扫描该段，凭唯一键和 event_key 防重复
8. 记录 sync_runs 与 warnings，释放同步锁
```

**关键细节**：分页时数据可能新增、排序可能移动。不能假定 `page=3` 是可以长期安全恢复的稳定游标。V1.1 默认在异常后重扫**尚未完成的整个有界时间段**，用幂等写入消除重复。不能仅保存 `page` 并跳过之前页面。对响应结果数量触及平台上限的窗口继续细分；仍无法完整枚举则保留 `partial` 和告警，禁止宣称“已完整采集”。

### 7.4 首次发现与更新的事件模型

| event_type | 触发条件 | 防噪规则 |
|---|---|---|
| `discovered` | 新建 Item | 仅第一次，唯一事件键 |
| `source_attached` | 已有 Item 新增关联来源 | 新增 Source 与原 Item 确认一致时产生 |
| `content_changed` | 关键字段的稳定内容哈希发生变化 | 不把单纯 stars、下载量、抓取时间变化当重大内容更新 |
| `code_detected` | 从未检测到实质代码线索转为检测到 | 同一事实重扫不重复事件；不宣称代码可运行 |

事件键建议由 `(event_type, stable_source_uid, normalized_change_fingerprint)` 计算 SHA-256；`discovered` 可基于 Item 创建时所用的第一个稳定来源标识生成。对同一来源恢复旧内容后又再次改变的场景，如需严格保留每一次转变，应结合来源版本号或连续状态变更序号，而不是仅用目标内容哈希；V1.1 默认只保证**同一事实重复扫描不重复生成事件**，不承诺完整历史审计能力。

`change_events.id` 单调递增。客户端保存自己的 `last_event_id`，按 `id > last_event_id` 查询并在业务处理成功后推进本地消费游标。`fetch_updates` 的事件列表若被 `limit` 截断，`next_after_id` **只能等于本页最后一条实际返回的事件 ID**，不能直接设置为数据库最新事件 ID；`max_available_event_id` 仅供显示与诊断，不得当作已消费游标。系统默认提供**至少一次可恢复读取**，不承诺推送端严格 exactly-once；日报应用需自行按 Event ID 去重。

### 7.5 Watch 刷新（不能只扫“今日新增”）

**主动追踪已有项目是单独的任务类型**。仅扫描最近两天新增的 HF Daily Papers、GitHub 仓库，无法保证一周前收藏的论文在今天补充代码时被发现。因此 `sync` 默认包含一个受预算限制的 `watch_refresh` 阶段：

1. 查询 `is_watched=1` 且最近检查时间超过配置周期的 Item，按最久未检查优先（默认建议每日复查一次）。
2. 对其已知 `arxiv`/`hf_paper`/`hf_model`/`github_repo` 来源调用轻量详情 API，重新提取 `githubRepo`、README/Model Card 的官方链接、仓库元数据与代码检测信号。
3. 为新发现的关联目标做 Source 验证、强弱证据判断、可能的 Source 挂接；在同一事务中写事件。
4. 一条记录检查失败不得将其 `last_checked_at` 更新为成功；按限流退避并留在待刷新队列，防止重复跳过。
5. `refresh_limit_per_run` 限制每次检查量，超预算的条目留到下次执行；这属于**有界追踪队列**，不是可宣称在当天全部刷新完成的采集水位线。

`watch_refresh` 的默认检查周期与最大吞吐需要根据平台配额调整。没有被关注、且不再出现在新增查询中的旧项目，V1.1 **不保证**能发现所有后续变化；可以在 P1 增加低频全库轮询。

### 7.6 事件和人工审核的组合

- 新 Item：自动 `inbox`，产生 `discovered`。
- `saved` + `is_watched=1`：仍返回具有价值的新来源/更新事件。
- `ignored`：默认不出现在普通候选列表中，不再反复创建 Item；仍保留去重映射。
- `saved` + `is_watched=0`：只保留基础元数据更新；不主动推送普通变化，是否查看历史事件由消费者决定。
- `is_watched` 的复查周期可由定时任务决定，V1.1 不在 `watch` 字符串中混入“是否已保存”。

---

## 8. 数据源适配器规范

### 8.1 共通 Adapter 契约

每个 Adapter 实现 `discover(query, window, page_token)`、`inspect(ref)` 与 `normalize(raw)`（不支持的功能明确抛出受控异常）。返回 `CandidateSource` 及独立分页元数据：

```python
class PageResult(BaseModel):
    records: list[CandidateSource]
    next_page_token: str | None = None
    page_complete: bool
    source_response_incomplete: bool = False
    warnings: list[str] = Field(default_factory=list)
```

分页结束不等于时间窗口完整：对照 `total_count`、服务端返回的完整性标记、当前硬上限、已有页数、查询超时等综合判断。每个 Adapter 对其源站 URL、ID、速率、排序字段负责；不可让通用网络层猜测业务语义。

### 8.2 arXiv

- API：`https://export.arxiv.org/api/query`；Atom 1.0，按类别和 `sortBy=lastUpdatedDate`/`submittedDate` 组合查询。
- 分页：`start` + `max_results`；`max_results` 建议 100–200，严控响应字节数。API 指南说明可分页，并建议连续调用之间至少留 3 秒间隔。
- 更新判断：分别保留 `published` 与 `updated`，并以已选采集策略覆盖新论文与修订论文。
- 时间窗口：尽可能以稳定的查询/排序和相邻重叠窗口扫描；如果 API 不能服务端精确过滤，就在客户端筛选，并在达到停止条件前继续分页。
- 解析：标准命名空间提取 `entry/id/title/summary/authors/published/updated/categories/links`；去换行和 HTML 片段，但保留可供溯源的原始摘要。
- 注意：arXiv 摘要通常不保证含官方代码链接，缺失链接不能说明代码不存在。

### 8.3 Hugging Face Daily Papers

- 基础服务可采用 `GET https://huggingface.co/api/daily_papers`；支持按日期和分页的能力可参考官方 `HfApi.list_daily_papers(date, p, limit, sort)` 文档，具体 HTTP 参数在开发时用集成测试验证。
- 每次按 UTC 目标日期迭代，不仅请求“最近 50 条”。对 HF 数据需保留平台 Paper 元数据的 `upvotes`，但点赞数**仅用于外部排序的参考，不构成真实性判断**。
- `hf_paper.source_uid = arxiv_id`，与 arXiv 同 ID 时属于强合并证据。
- 平台标注的 `githubRepo` 作为官方代码**线索**；与 repo 反向证据吻合后才达到 V1.1 自动跨实体归并标准。

### 8.4 Hugging Face Models

- 列表/详情：Hub API 的 models 列表和 `/api/models/{repo_id}`；模型仓库 README/Model Card 可通过合法公开地址读取。
- 可设置 `recently_modified` 与 `trending` 两种扫描模式；`trending` 属于排名快照，不能单靠它宣称“枚举了所有新增模型”。
- 收录 `likes/downloads/pipeline_tag/tags/lastModified` 等基础字段；不下载模型权重。
- HF model 引用某篇论文属于 `official_model` 或 `related` 关系的候选证据，单向引用不自动合并论文和模型来源。
- 模型 README 可能不存在、非 main 分支或未授权，区分 `not_found`、`forbidden`、`unavailable`，不能把 404 统一判为“空壳”。

### 8.5 GitHub

- 检索：`GET /search/repositories`；分别建立 `recently_created` 和 `recently_updated` 查询作业。
- `created` 便于发现新仓库；`pushed`/`updated` 用于发现近期活跃或补开源仓库，字段语义按实际 Search 支持的限定词构建并通过测试验证。
- 存储 GitHub 数字 `id`、`full_name`、`html_url`、`default_branch`、`created_at`、`pushed_at`、`updated_at`、`stargazers_count`、`license`。
- **搜索限制**：REST Search 单个查询最多 1,000 个结果；`incomplete_results=true` 或达到上限时必须缩小时间窗/查询条件，仍不完整则标记 `partial`。
- **双重限流**：Search 有独立配额；普通 REST API 还有主速率与次级速率限制。优先遵循返回的 `Retry-After`、`X-RateLimit-*`，不要只写固定指数重试。
- `inspect`：仓库详情 + 根目录 `contents` + README（带响应大小限制）。对根目录清单优先从 `/repos/{owner}/{repo}/contents`，必要时在只读且限深度的代码探查中补充 `/git/trees`，不能递归整个仓库。
- 仓库类型：`fork`、`archived`、空仓库和 PR/Issue 链接应分类处理，**不能仅靠 README、Star 数量给出真假结论**。

---

## 9. 基础源码检测与内容提纯

### 9.1 `inspect_source` 的能力边界

仅做**低成本来源探针**：不 clone、不 pip install、不执行仓库代码、不下载大文件或权重，不检查 CUDA/ROS 版本，也不输出“本地可部署”。调用方得到的是原始来源事实与有限启发式结论。

### 9.2 GitHub `code_status` 规则

| 情况 | 返回 |
|---|---|
| API 不可访问、目录结构未知 | `unknown` |
| 可访问根目录，但只看到了 README、LICENSE、图片等非代码文件 | `not_detected`（不是“没有代码”的断言） |
| 发现可信代码文件、源码目录或项目构建文件 | `detected` |

若仅有 `src/` 空目录、占位脚本、自动生成文件等，则允许仍是 `detected`，但必须附 `signals` 和 `limitations`；不能宣称实现完整或训练可复现。建议输出 `root_files`、`code_signals`、`checked_depth`、`checked_at` 和 `warnings`。

### 9.3 内容截断

- 摘要/简介：标准化空白字符；`Candidate` 中最多 400–800 字符作为检索预览。
- README/Model Card：正文限 3,000–5,000 字符；抓取体积上限独立设置，防止先下载超大文件再截断。
- 原始来源链接、标题、作者等元数据必须与截断后的文章简介区分保存。
- 不执行 Markdown 中的 Shell 命令、HTML、远程图片或任意指令；大模型读取时将内容标为“未可信外部资料”。

---

## 10. 服务 API、MCP Tools 与 CLI 契约

### 10.1 通用响应与错误约定

成功返回 `ok=true`、`data`/`items`/`events` 和相关 `stats`；可降级时携带 `warnings`。错误返回稳定 `error.code`、`error.message`、可选 `retry_after_seconds`，**不把 Python traceback 当作工具结果**。内部日志应含 `run_id`、`job_key`、`source_type`、`error_code`；永不包含 Token。

错误码示例：`INVALID_TARGET`、`UNSUPPORTED_SOURCE`、`NOT_FOUND`、`UPSTREAM_TIMEOUT`、`RATE_LIMITED`、`PARTIAL_SYNC`、`MERGE_CONFLICT`、`DB_BUSY`、`SCHEMA_MISMATCH`。

### 10.2 核心工具 1：`fetch_updates`

**语义**：执行一次同步（有持久化副作用），返回该次发现的新变化事件；不会因未调用 `save_item` 而丢失去重状态。默认仅面向短时间窗；对于大规模历史回填，优先由 CLI 执行。

**输入**：

```json
{
  "days": 2,
  "feeds": ["arxiv", "hf_papers", "hf_models", "github"],
  "topics": ["VLA", "Diffusion Policy"],
  "limit": 30
}
```

- `days`：1–14，仅定义手动请求回溯范围；定时任务按内部水位线构建窗口。
- `feeds`：可选，缺省全部启用来源；`topics` 可覆盖查询主题。
- `limit`：1–100，仅约束返回事件条数，不应中断完整采集。
- 目标 `job_key` 由最终规范化查询计算，避免一次临时主题覆盖周期作业游标。

**响应示例（纯示意）**：

```json
{
  "ok": true,
  "data": {
    "run_id": 120,
    "status": "success",
    "events": [
      {
        "id": 931,
        "item_id": 42,
        "event_type": "source_attached",
        "source": {"source_type": "github_repo", "external_id": "lab-ai/repo"}
      }
    ],
    "next_after_id": 931,
    "max_available_event_id": 931,
    "has_more": false
  },
  "stats": {
    "fetched": 128,
    "new_items": 17,
    "new_sources": 20,
    "new_events": 21,
    "conflicts": 1,
    "completed_feeds": 4,
    "partial_feeds": 0
  },
  "warnings": []
}
```

多 Feed 仅有部分成功时，`status="partial"`；不能因为 `ok=true` 就误解为“所有来源完整采集”。`ok=true` 只表示本次工具调用得到**结构化且可用的部分结果**。`completed_until`、错误可通过状态接口查询。`next_after_id` 为本次返回页最后一条事件 ID（若无事件则返回请求游标），仅当消费者成功处理返回列表后方可记录为已消费；`max_available_event_id` 是数据库目前可见的最新事件 ID，仅用于诊断。

### 10.3 核心工具 2：`inspect_source`

输入 `{ "target": "https://github.com/owner/repo" }`，或 arXiv ID、HF model URL。返回 `SourceRef`、标题、简介、发布/更新时间、`code_status`、`root_files`、`signals`、`extracted_links`、`content_excerpt`、`warnings` 与检测时间。只有已知白名单来源才允许发起外部 HTTP 请求，禁止接受任意网页 URL 并直接访问。

### 10.4 核心工具 3：`save_item`

**修订语义**：对已存在 Item 执行审核、分类、人工备注与关注设置；兼容“从手动 URL 新建实体”，但新建必须先经过 Source 的平台验证与稳定 ID 解析。

```json
{
  "item_id": 42,
  "triage_status": "saved",
  "category": "robotics-policy",
  "is_watched": true,
  "notes": "重点跟踪是否发布真实机器人复现实验",
  "expected_revision": 3
}
```

- `item_id` 和 `expected_revision` 用于乐观并发控制，防止旧客户端覆盖新编辑。
- 自动发现创建 Item 的行为由 `fetch_updates` 完成，无须先调用 `save_item`。
- `notes` 必须明确是**覆盖**还是**追加**；V1.1 默认覆盖，并提供 CLI 的 `--append-note` 安全选项。
- 若要从来源新建，使用 `source` 参数并保持与 `item_id` 互斥；源命中多个 Item 返回 `MERGE_CONFLICT`，不隐式合并。
- 返回 `item_id`、`action:created|updated|unchanged`、`revision`、`triage_status`。

### 10.5 核心工具 4：`search_items`

输入：`keyword`、`triage_status`、`is_watched`、`code_status`、`category`、`first_seen_after`、`updated_after`、`limit`（1–100）、`cursor`。输出 Item 列表、匹配总数（可选，若成本高可标记 approximate）、`next_cursor`。默认按 `(updated_at DESC, id DESC)` 稳定排序，推荐键集分页；搜索不能简单取前 N 条并宣称完整。

### 10.6 核心工具 5：`list_events`

输入：`after_id=0`、`limit=50`、`event_types?`、`include_ignored=false`、`only_watched?`；按 `change_events.id ASC` 返回，包含 `next_after_id` 与 `has_more`。外部日报/推送系统维护自己的消费游标。**本工具不修改 Event 消费状态**，避免不同 Agent 相互覆盖进度。

### 10.7 只读辅助工具：`get_sync_status`

返回各 `job_key` 的 `completed_until`、`last_success_at`、`last_status`、最近运行统计及告警。用于解释“最近没有新内容”究竟是“确实无新结果”还是“源站失败”。

### 10.8 CLI 规范

```bash
# 本地运行定时增量同步（自动入库）
uv run info-desk sync

# 明确回溯窗口：独立的临时作业，不推进日常同步水位线
uv run info-desk sync --days 7 --feed arxiv --topic "VLA"

# 读取事件，而不是依赖上一次同步过程的控制台输出
uv run info-desk events --after-id 930 --limit 50

# 查看具体来源及系统状态
uv run info-desk inspect https://github.com/owner/repo
uv run info-desk status
uv run info-desk doctor

# 人工审核及查找
uv run info-desk triage 42 --status saved --watch
uv run info-desk search --status saved --keyword "diffusion"

# 严格受控的冲突管理
uv run info-desk conflicts list
uv run info-desk conflicts resolve 12 --winner 42

# stdio MCP
uv run info-desk serve --transport stdio
```

CLI 无法调用外部 MCP 客户端时，所有业务功能仍可独立测试。`serve --transport stdio` 期间禁止向 stdout 发送除 MCP 协议之外的文本；仅 stderr 或文件日志。

---

## 11. 异常、并发、数据安全与资源限制

### 11.1 网络客户端

- 单一 `httpx.AsyncClient` 管理连接；每个域名独立并发信号量和速率限制。
- 请求必须有 connect/read/write/pool 超时，单次响应字节上限、最大分页次数和总任务时限。
- `429`：优先 `Retry-After`；GitHub 同时查看主/次级速率响应；`5xx` 和临时网络错误可做有限次数指数退避加抖动。
- `401/403`：区分凭证错误、访问拒绝和配额耗尽；凭证错误不要无限重试。
- 任何重试达到预算后产生 `partial/failed` 并保留尚未完成时间段。
- 只对幂等 GET 自动重试；不得重复提交非幂等人工变更。

### 11.2 并发

- SQLite WAL 允许读写并行，但**仍只有一个写事务可同时进行**。
- 同一 `job_key` 使用进程级独占锁；不同作业共享数据库短事务，必要时统一由单写队列提交。
- 同一台机器可能存在多个 MCP Server 进程。至少保证 Source/事件唯一键与事务检查，防止重复插入；无法获取同步独占锁时返回 `SYNC_ALREADY_RUNNING`。
- 用 HTTP I/O 读写时绝不持有 SQLite 事务；归并决策和入库时应在 `BEGIN IMMEDIATE` 后重新验证关键身份关系，避免竞态。

### 11.3 凭证与配置

- 读取优先级：环境变量 `GITHUB_TOKEN`、`HF_TOKEN` > 本地配置；示例配置不得包含真实 Token。
- 配置文件与 DB 文件权限建议 `0600`，数据目录 `0700`；日志脱敏所有 Authorization/cookie/query secret。
- GitHub/HF Token 只授予访问公开资源必要的最小权限。默认不推送代码、不建立公开仓库。

### 11.4 SSRF 与恶意来源文本

`inspect_source` 限制为 `arxiv.org`、`export.arxiv.org`、`github.com`、`api.github.com`、`huggingface.co` 等明确允许的官方 HTTPS 域名与预期路径。禁止自定义任意 URL/端口；重定向每一跳重新验证 host、scheme、DNS/本地地址限制和最终路径。外部 README 中的链接只保存为文本证据，不应无条件再次抓取。为解析器设置文档尺寸、嵌套深度、循环跳转与文本长度限制。

来自 README/摘要/Model Card 的内容是**不可信资料**。服务不得执行其中的指令。向大模型返回时要标记来源，不允许资料中的“请忽略之前指令”等文本决定后续工具行为。

---

## 12. 配置与运行目录

### 12.1 配置范例（`config.example.toml`）

```toml
[data]
home = "~/.information-desk"
database = "intel.db"

[sync]
feeds = ["arxiv", "hf_papers", "hf_models", "github"]
topics = ["LLM", "Agent", "VLM", "VLA", "Diffusion Policy"]
overlap_hours = 48
initial_lookback_days = 2
max_pages_per_window = 20
max_events_per_response = 30
watch_refresh_interval_hours = 24
watch_refresh_limit_per_run = 100

[arxiv]
categories = ["cs.AI", "cs.CL", "cs.LG", "cs.CV", "cs.RO"]
min_request_interval_sec = 3.0
page_size = 100

[github]
search_created = true
search_recently_updated = true
page_size = 100

[huggingface]
daily_papers = true
models = true

[http]
connect_timeout_sec = 5
read_timeout_sec = 20
max_response_bytes = 4000000
max_retries = 3

[logging]
level = "INFO"
max_file_mb = 10
backup_count = 3
```

默认路径 `~/.information-desk/`，环境变量 `INFO_DESK_HOME` 可覆盖，建议统一结构：

```text
~/.information-desk/
├── config.toml
├── intel.db
├── logs/information-desk.log
├── backups/
└── run/
```

`max_pages_per_window` 是防滥用阈值，**达到阈值必须标记 `partial`**；不可理解为采集完整的页数。Token 仅从进程环境或密钥管理器注入，配置范例不得要求明文填写 Token。

### 12.2 定时运行

默认只需要 **一个定时同步入口**，由系统服务管理；它依次或按配额并发执行各平台增量 Job 与有界 `watch_refresh`，内部在 Sync Engine 中按规范限流。Linux 可使用 `systemd --user` timer 并设置 `Persistent=true`，以补偿关机期间漏掉的执行时间；定时器负责唤醒，水位线负责数据连续性，Watch 刷新则由独立的下次检查时间/剩余队列控制，三者职责分离。

建议初始周期：每 6 小时执行一次；arXiv/HF Daily Papers 可以根据实际发布周期减频，GitHub 视 API 配额调度。避免原设计中多个外部定时任务并发操作同一 SQLite 状态导致冲突。

未来与 OpenClaw、DSH、n8n 或日报系统集成时，优先调用 `list_events`（使用各自游标），不要读取数据库内部表作为私有协议。

---

## 13. 可观测性与诊断

### 13.1 必须记录的指标

- 采集：`job_key`、Feed、查询/时间窗口、页数、API 状态、抓取数量、成功范围、未完成原因。
- 归并：新 Source、新 Item、自动关联数、弱关系数、冲突数。
- 事件：新增事件数、按类型分布、重复事件抑制数。
- 质量：读取失败数、空壳探针结果数、无效 ID/外部文本解析失败数。
- 资源：请求次数、重试次数、Rate Limit 命中、数据库大小、SQLite Busy 次数、任务耗时。

### 13.2 状态诊断规则

| 现象 | 解释 | 操作 |
|---|---|---|
| 本次 `new_items=0` 且全部 feed `success` | 已扫描范围内无新实体 | 可正常返回无新增 |
| 本次 `new_items=0` 但 feed `partial` | 无法说明是否有新实体 | 输出明确告警，后续重试 |
| GitHub `incomplete_results=true` | Search 结果可能不完整 | 切更窄时间窗口，仍失败时保留 partial |
| 持续产生 merge conflict | 归并规则可能过宽或领域实体定义有冲突 | 暂停对应自动合并规则，审查证据 |
| 日报重复展示 | 下游 Event ID 游标没有持久化或确认机制 | 修复下游游标/幂等消费 |

日志文件轮转、错误码标准化；`doctor` 至少检查配置、数据库迁移版本、目录权限、FTS5 可用性、MCP SDK 主要版本，以及源站连通性（网络探测可选）。

---

## 14. 测试矩阵与发布验收

### 14.1 单元测试

1. **标识**：arXiv `v1/v2`、PDF 链接、legacy ID、GitHub 大小写/`.git`/`tree`、HF papers/models/datasets 分流。
2. **归并**：arXiv + HF Paper 同 ID；双向官方代码；普通引用不可合并；一仓多论文不得错误连通合并。
3. **持久化**：同 Source 重复 upsert，`source_uid` 唯一；GitHub 重命名后不新建 Source；不同 Item 强证据冲突不丢笔记。
4. **状态机**：`saved+watched` 可更新；`ignored` 不重复出现在普通候选；人工笔记不被自动覆盖。
5. **事件**：同次重试不重复；新代码产生 `source_attached`/`code_detected`；stars 变化不生成重要内容事件。
6. **安全**：Token 脱敏、任意 URL 拒绝、重定向到 localhost 拒绝、恶意 Markdown 不执行。
7. **追踪**：一周前的已关注 HF Paper 今天补充官方 GitHub 链接，即使不在最近 Daily Papers 日期窗口内，也能经 `watch_refresh` 发现。

### 14.2 集成与故障注入

- 使用本地 HTTP Mock/Fixture 对 arXiv Atom、HF JSON、GitHub REST 分页进行离线测试。
- 第 1、2 页成功，第 3 页超时：该窗口不能推进 `completed_until`；重跑后不得重复建表。
- 提前返回 1,000 条、`incomplete_results=true`：强制拆分窗口或 `partial`。
- SQLite 事务前/后模拟进程中断：重启后一致性正确、事件不重复。
- 两个进程同时 `sync` 同一 `job_key`：只能一个执行，另一返回可诊断受控错误。
- `save_item(expected_revision=旧值)`：返回并发冲突，不覆盖新笔记。
- MCP v2 客户端列出全部工具并调用一次；stdio stdout 无噪音。

### 14.3 最低验收门槛（Release Gate）

| 编号 | 通过条件 | 级别 |
|---|---|---|
| A01 | 论文同时出现在 arXiv/HF Papers，稳定归为单一 Item | P0 |
| A02 | README 单向引用不触发错误归并，两个已有 Item 冲突不会覆盖数据 | P0 |
| A03 | 同一窗口完整重跑，不重复创建 Item/Source/事实事件 | P0 |
| A04 | 第三页超时/限流时状态为 partial 且未错误推进水位线 | P0 |
| A05 | 已 saved 的论文次日出现 GitHub 官方实现，能查询到更新事件 | P0 |
| A06 | 忽略后不重复进入普通候选，人工笔记及分类保留 | P0 |
| A07 | CLI 可完全离线自测核心服务；MCP v2 客户端可列出并调用工具 | P0 |
| A08 | SSRF 白名单、Token 脱敏、输入长度限制、异常错误码通过测试 | P0 |
| A09 | `list_events(after_id)` 按稳定顺序分页，两个消费者游标互不影响 | P0 |
| A10 | FTS5 可用则建立全文索引；不支持时可降级检索 | P1 |
| A11 | 故障后恢复、备份恢复、schema 迁移与 `doctor` 执行正确 | P0 |
| A12 | 老论文超出新发现日期窗后出现新官方代码，关注队列仍可捕获 | P0 |

**验收状态**：本仓库已有离线、契约和有界 live 验收证据，逐项结果及边界见 [`docs/validation-results.md`](docs/validation-results.md)。目标 MCP 宿主互操作和更长时间的跨重启定时运行仍需部署方环境验证；表格本身不替代该记录。

---

## 15. 与原 V1.0 草案的迁移/差异表

| 原 V1.0 设计 | V1.1 修订 | 原因 |
|---|---|---|
| `fetch_updates` 只返回候选，等 `save_item` 落库 | 采集即入库，并返回 Event | 不依赖智能体成功调用二次写入，杜绝重复候选 |
| `inbox/saved/watch/ignored` 四状态 | `triage_status` + `is_watched` + `code_status` | 审核、追踪和源码检测是不同维度 |
| Source 主键仅 `(source_type, source_id)` | 来源行 ID + 稳定 `source_uid` + 可读 `external_id` | GitHub 仓库重命名可追踪 |
| 共享任意链接即连通合并 | 关联证据等级 + 冲突持久化 | 降低错误归并及人工数据丢失 |
| `INSERT OR REPLACE` 更新来源归属 | 短事务、显式冲突处理 | 防止来源漂移与笔记覆盖 |
| 有 `days` 参数，但无持久化水位线 | `sync_state`、分段完整性、重扫重叠窗口 | 支持长期增量与异常恢复 |
| 已保存实体整组过滤 | 可产生 `source_attached`/`content_changed` | 让开源补档、模型补档可被发现 |
| 只有 4 个 MCP 工具 | 增 `list_events`、`get_sync_status` | 便于日报和运维可靠消费 |
| `FastMCP` | 官方 MCP SDK v2 `MCPServer` | 修复 v1/v2 导入兼容性 |
| 默认配置可存 Token | 环境变量优先且无明文示例 | 降低秘密泄露风险 |

如果**已存在旧数据库**：新表不能简单用 `CREATE TABLE IF NOT EXISTS` 覆盖旧表。迁移脚本必须显式识别旧 `items`/`sources` 的列集合，重建数据表并生成迁移报告；旧 `status='watch'` 映射为 `triage_status='inbox', is_watched=1`（如已有用户明确将其保存，应优先人工核对），旧 `has_code` 转换为 `code_status='detected'/'unknown'`，旧 Source 的 GitHub 可读路径需逐条 API 查询数字 ID，查询失败保持待解析状态而不能伪造稳定 ID。对一对多映射冲突保留原始记录并人工解决。**任何迁移前必须备份，测试副本通过后再迁移生产库。**

---

## 16. 施工顺序与里程碑

### M0：配置和环境（基础）

- 建包结构、`uv.lock`、配置模板、统一错误模型、日志、SQLite 迁移器。
- 在当前部署设备验证 `mcp>=2,<3` 的客户端连通性，固定兼容版本。

### M1：存储及身份归并（P0）

- 完成 7 张核心表、索引、Repository、唯一约束和人工数据保护。
- 完成 URL/ID 规范化、关联证据分级、冲突队列、幂等写入。
- 写完相关离线单测，再进入采集。

### M2：采集和事件（P0）

- 实现网络客户端及 4 个 Adapter、分段分页、Job 水位线和重叠窗口。
- 实现“采集即入库”和四类 Event、断点重试、采集完整性报告。
- 构造 API 分页失败、Search 上限、重复抓取的故障注入测试。

### M3：工具与验收（P0）

- 实现 MCP/CLI，业务逻辑统一调用 Service。
- 验证 `fetch_updates`、`inspect_source`、`save_item`、`search_items`、`list_events`、`get_sync_status`。
- 集成 `systemd` timer、备份、`doctor`、真实 API 小规模冒烟测试；达到 A01–A09/A11/A12 发布门槛。

### M4：有限增强（P1，视实际需要）

- FTS5 中文检索改进、查询优化、简单事件评分、人工归并操作体验。
- 后续再接科研日报、智能体自动筛选、RSS/Semantic Scholar/OpenReview，而非在 V1.1 混入全部特性。

开发验收完成后建议形成以下交付物：源代码仓库、版本锁定、测试报告、真实 API 冒烟日志、示例配置、数据迁移与备份说明、MCP 客户端配置样例。

---

## 17. 风险清单及明确不承诺的能力

| 风险 | 影响 | 缓解 / V1.1 边界 |
|---|---|---|
| GitHub Search 上限、排序变化、索引延迟 | 漏收 | 切分时间窗、重叠扫描、`partial` 状态；不承诺全球完全覆盖 |
| arXiv 与 HF 日程及 API 更新时间不同 | 窗口边缘漏收 | 按源调整更新时间字段和重扫窗口 |
| 官方链接缺少反向证据 | 可能暂时重复实体 | 优先保守、保留待确认关系 |
| 一个仓库对应多篇论文 | 连通归并污染 | 弱关联只建 Link，多实体冲突拒绝自动合并 |
| 作者改名、仓库转移、HF 模型仓库搬迁 | 重复或错配 | GitHub 数字 ID；HF 仅尽力识别，无稳定 ID 不保证自动迁移 |
| 根目录探针不完整 | 错判“没有代码” | 使用 `not_detected`，不输出“无代码/可复现”结论 |
| 多个外部应用同时读取事件 | 重复消费 | 以各自 `last_event_id` 做至少一次幂等消费 |
| sqlite 数据损坏/磁盘损坏 | 丢库 | WAL、定期 backup、演练恢复 |
| MCP 外部文本提示注入 | 非预期工具调用 | 文本不可信、白名单抓取、服务端不执行外部命令 |

---

## 18. 官方资料与设计依据

以下为截至 **2026-11-13** 核对的上游文档；设计中的窗口长度、模块分层、证据等级、事件模型与数据 Schema 为本项目工程决策，并非上游 API 的官方承诺。

1. MCP Python SDK v2 官方文档：https://py.sdk.modelcontextprotocol.io/  
   v1→v2 迁移：https://py.sdk.modelcontextprotocol.io/migration/  
   关键点：`FastMCP` 改为 `MCPServer`，`from mcp.server import MCPServer`。
2. arXiv API User's Manual：https://info.arxiv.org/help/api/user-manual.html  
   关键点：`start`、`max_results` 分页、`sortBy` 选项与连续请求约 3 秒间隔建议。
3. arXiv API Terms of Use：https://info.arxiv.org/help/api/tou.html
4. Hugging Face `HfApi.list_daily_papers`：https://huggingface.co/docs/huggingface_hub/en/package_reference/hf_api  
   关键点：`date`、`p`、`limit`、`sort`。
5. GitHub REST Search：https://docs.github.com/en/rest/search/search  
   关键点：单查询最多 1,000 条、`incomplete_results`、Search 专用配额。
6. GitHub REST Rate Limits：https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api  
   关键点：`Retry-After`、`X-RateLimit-*`、主/次级限流。
7. Python SQLite：https://docs.python.org/3/library/sqlite3.html

---

## 附录 A：关键开发决策（ADR）

| ADR | 决策 | 替代方案及暂不采用原因 |
|---|---|---|
| ADR-001 | SQLite + WAL | PostgreSQL/Redis 对 V1.1 单机规模无必要 |
| ADR-002 | 采集时自动持久化 `inbox` | 先只返回候选再保存会造成重复、遗漏、依赖外部 Agent |
| ADR-003 | 保守证据归并 + 显式冲突 | 无条件 union-find 连通分量会错误吞并多个研究实体 |
| ADR-004 | 分段扫描 + 重扫不完整区间 | 直接恢复页码在动态搜索结果里可能跳项 |
| ADR-005 | 事件表 + 消费者独立游标 | 直接依赖 fetch HTTP/MCP 单次返回不能保证故障后恢复 |
| ADR-006 | MCP v2 `MCPServer` + stdio | V1 SDK 导入不兼容；HTTP 对本地 MVP 增加安全/部署面 |
| ADR-007 | 不用 LLM 判定论文与代码真假 | 此任务强调来源治理，不额外引入模型成本与不确定性 |
| ADR-008 | 归并人工操作只开放管理 CLI | 避免 MCP 客户端误触发不可逆实体重组 |

## 附录 B：待实现前最后核定的参数

以下默认值是**工程建议，不是源站官方保障**，应按实际网络/API 数据进行调整：`overlap_hours=48`、`watch_refresh_interval_hours=24`、`initial_lookback_days=2`、`max_pages_per_window=20`、`arxiv.page_size=100`、`GitHub.page_size=100`、`README_excerpt=3000–5000 chars`、`HTTP 最大响应=4MB`。核定时必须执行：一天实际数据量抽样、不同页面排序稳定性检查、HF Models 扫描可枚举性评估、最大一次扫描耗时与 API 配额评估。

**设计完成条件**：本文明确了边界、数据契约、表约束、跨源归并、同步水位线、事件消费、API/CLI、失败恢复、安全、部署和验收；开发完成条件以第 14 章的实际测试结果为准。
