# information-desk 项目结构与实施计划

> 状态为待实现。本文细化 [V1.1 架构基线](../information-desk-architecture-v1.1.md)的目录建议。工具输入、响应和行为以[智能体接口设计](mcp-interface.md)为实施契约。

## 1. 目录规划

以下为目标布局。现有架构基线保留在根目录，按实施阶段新增其余文件。

```text
information-desk/
├── information-desk-architecture-v1.1.md
├── README.md
├── pyproject.toml
├── uv.lock
├── .gitignore
├── config.example.toml
├── docs/
│   ├── mcp-interface.md
│   ├── project-structure.md
│   └── operations.md
├── src/info_desk/
│   ├── __init__.py
│   ├── app.py
│   ├── cli.py
│   ├── server.py
│   ├── config.py
│   ├── contracts.py
│   ├── domain.py
│   ├── exceptions.py
│   ├── service.py
│   ├── sync_engine.py
│   ├── watch_refresh.py
│   ├── resolver.py
│   ├── inspect.py
│   ├── http_client.py
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── arxiv.py
│   │   ├── hf_papers.py
│   │   ├── hf_models.py
│   │   └── github.py
│   └── storage/
│       ├── __init__.py
│       ├── sqlite.py
│       ├── repository.py
│       ├── migrations.py
│       └── sql/
│           └── 001_initial.sql
├── tests/
│   ├── fixtures/
│   │   ├── arxiv/
│   │   ├── hf_papers/
│   │   ├── hf_models/
│   │   └── github/
│   ├── unit/
│   │   ├── test_contracts.py
│   │   ├── test_resolver.py
│   │   └── test_sources.py
│   ├── integration/
│   │   ├── test_storage.py
│   │   ├── test_sync.py
│   │   └── test_watch_refresh.py
│   └── contract/
│       ├── test_cli.py
│       └── test_mcp_stdio.py
└── deploy/
    ├── information-desk.service
    └── information-desk.timer
```

## 2. 模块职责与依赖

| 位置 | 职责与边界 |
|---|---|
| `pyproject.toml` | 定义 Python 3.11+、依赖范围、测试配置及入口 `info-desk = info_desk.cli:main`。将 `storage/sql/*.sql` 纳入 wheel |
| `app.py` | 组装配置、Repository、HTTP 客户端、适配器及 Service。按 CLI 命令和 MCP 模式初始化所需资源，统一管理生命周期 |
| `cli.py`、`server.py` | 将命令行或 MCP 调用映射到 Service。CLI 负责退出码和展示，MCP 负责工具注册、参数与输出 schema。两层均通过同一契约验证 |
| `config.py` | 加载 TOML 与环境变量，校验运行目录、启用来源、预算和限速。运行数据使用 `INFO_DESK_HOME` 或默认目录 |
| `contracts.py` | Pydantic 请求、公开结果、通用错误与告警。保持 MCP schema、CLI JSON 和 Service 的输入输出含义一致 |
| `domain.py`、`exceptions.py` | 定义实体、来源、证据、事件、分页结果和业务异常。领域对象承载业务事实，公开结果由 Service 映射 |
| `service.py` | 实现查询、事件读取、状态、来源探查、人工变更、刷新和管理用例。负责调用顺序、事务边界及业务错误映射 |
| `sync_engine.py` | 规范化作业、切分窗口、评估完整性、持久化每页与运行状态。负责周期和手动命名空间、进程间同步锁 |
| `watch_refresh.py` | 根据来源上次成功检查时间选取到期条目，按预算探查新关联并入库 |
| `resolver.py` | 归一化来源身份、评估证据与归属。依赖已取得的领域记录和查库结果，归并决策在写事务中校验 |
| `inspect.py` | 将标准来源目标路由到对应适配器，整理代码信号和证据预览 |
| `sources/base.py` 与各源模块 | 定义发现、探查和规范化契约，各适配器负责平台分页、完成判据、请求参数及结果解析 |
| `http_client.py` | 负责域名和重定向策略、响应上限、超时、限流、有限重试和日志脱敏 |
| `storage/sqlite.py` | 管理连接、事务及 SQLite 配置，提供只读连接和写事务入口 |
| `storage/repository.py` | 维护查询、幂等 upsert、事件与运行记录、人工字段的条件更新 |
| `storage/migrations.py` 与 `storage/sql/` | 管理 schema 版本、迁移和回滚。初始脚本以 V1.1 DDL 为基础，落实新增的持久化契约 |
| `deploy/`、`docs/operations.md` | 用户级定时同步和安装、宿主接入、诊断、备份恢复流程 |

依赖方向为 `CLI/MCP → Service → 采集、归并、探查、Repository → 基础设施`。领域及公开契约在底层独立定义。适配器返回规范化对象给采集引擎，采集引擎通过 Repository 持久化。只有入口层和组装入口感知 CLI 或 MCP。

## 3. 存储与部署设计落点

| 设计决策 | 落地要求 |
|---|---|
| 周期同步可恢复 | 持久化 `coverage_from`、`completed_until` 与当前待重扫分段的边界。初次运行在发请求前记录起点；失败后从原区间重扫。区间完成记录与水位线更新一起提交 |
| 手动刷新独立计量 | 单次请求使用根运行 ID，子作业记录归属同一运行。事件写入时记录产生它的运行 ID，手工导入等非同步事件允许为空。事件预览用运行 ID 取样，消费方按全局 Event ID 读取 |
| 进程间互斥 | 锁文件位于数据目录的 `run/`，Linux 使用非阻塞 `flock`。刷新和周期同步共享同一 feed 锁，获取顺序固定。关注刷新按来源所属 feed 取得同一把锁，锁占用时保留待检查项 |
| 审核保护 | 事务内按 `item_id + revision` 校验审核变更。自动同步只写自动管理字段。展示字段或来源发生实质变化时更新 `updated_at`；变更人工可编辑字段的路径均维护 `revision` |
| 事件快照 | 在 `change_events.payload_json` 持久化当时的来源字段和事件载荷，以便仓库改名后仍能追溯原事件。合并后的事件保留 ID |
| 分页与检索 | 初始迁移包含 Item 排序、来源身份及发布日期查询索引。FTS5 可用时建立全文索引并在同一写事务维护，查询端提供 LIKE 降级 |
| 运行目录 | 数据库、日志、锁、备份置于 `INFO_DESK_HOME` 下，开发和测试通过临时目录隔离。部署凭证通过环境注入 |

同一台主机上的周期任务、CLI 和 MCP 实例使用相同的数据目录及锁文件。SQLite 写事务只覆盖本地决策和提交，联网请求在事务外执行。各进程的 HTTP 客户端管理各自的连接，采集锁负责串行化同一 feed 的同步。探查与导入也受单次调用的限速和请求预算约束，跨进程全局配额协调可在确认有需要后加到共享请求调度。

## 4. 本机开发与运行入口

本机默认使用 Conda 的 `mujoco` Python。通过 `uv` 创建项目环境并锁定依赖。以下命令在项目骨架及入口实现后使用：

```bash
mujoco_python="$(conda run -n mujoco python -c 'import sys; print(sys.executable)')"
uv sync --locked --python "$mujoco_python"
uv run --locked pytest
uv run --locked info-desk doctor
uv run --locked info-desk serve --transport stdio --profile read-only
```

MCP 宿主启动配置使用已同步项目环境中 `info-desk` 的绝对路径，向进程传入 `INFO_DESK_HOME`。这样宿主无需先加载交互式 shell。定时器调用同一个已安装入口，使用同一数据目录。将环境准备、初始迁移及实际目标宿主配置记录在 `docs/operations.md`。

## 5. 实施顺序与验收

| 阶段 | 实现范围 | 验收条件 |
|---|---|---|
| M0 · 可安装骨架 | 包、依赖锁、配置、迁移和 `doctor` | 在 `mujoco` 解释器上创建项目环境；安装后的 wheel 能找到迁移 SQL；临时库初始化通过 |
| M1 · 离线闭环 | Repository、身份归并、事件、查询与作业状态；用 arXiv fixture 驱动同步 | 重跑不重复建实体或事件；分页失败保留已提交页面，重启后完整重扫未完成区间；读接口稳定分页 |
| M2 · 智能体查询 | 发布只读 MCP 工具与 CLI JSON 输出 | stdio 只注册三个只读工具；实际输入和输出符合 schema；可解释空结果与部分覆盖 |
| M3 · 多源及写入 | 四源适配器、关注刷新、探查、人工审核、手动刷新 | 同论文正确归并；迟到强证据进入冲突队列；旧论文补代码产生事件；并发写入与重试安全；修订冲突可恢复 |
| M4 · 部署验收 | 定时器、备份恢复、真实源站冒烟、目标 MCP 宿主验证 | 通过 V1.1 第 14 章 P0 验收。用真实宿主走查查找、事件续读、收藏及部分刷新 |

`tests/unit/` 验证参数边界、身份规则和各源解析。`tests/integration/` 使用临时 SQLite 和模拟 HTTP 验证事务、分页、进程并发与故障恢复。`tests/contract/` 启动真实 CLI 与 stdio 子进程，比较实际工具列表及结果 schema，并测试两种模式与错误反馈。默认测试离线运行，真实源站冒烟单独执行。

## 6. 与架构基线的实施差异

- 工具名称沿用 V1.1。MCP 默认只注册三个本地读工具，`standard` 注册全部六个。
- `fetch_updates` 由原有事件页改为运行回执及事件预览，全局续读统一使用 `list_events`。
- `search_items` 增加精确 Item ID 和来源发布日期过滤。公开对象使用 `item_id`，以便跨工具直接传递。
- 原作业锁收紧为共享 feed 锁，防止同源的周期任务和手动刷新并行消耗配额。周期和手动任务仍独立记账。
- 初始迁移在基线 SQL 上补充首次窗口、重扫分段、运行归属及事件快照的持久化字段。目录规划增加 `app.py`、`contracts.py` 和 `watch_refresh.py`，分别归置应用组装、公开契约和关注队列。
