# V1.1 工程测试与发布验收

验收记录日期：2026-11-13（America/Los_Angeles）。本报告只记录实际运行结果；SDK 子进程测试不代表连接过特定智能体宿主。

## 测试环境与结果

| 检查 | 实际结果 |
|---|---|
| Python 3.11 | CPython 3.11.18；离线套件 118 passed、1 live test deselected；行覆盖率 86.82% |
| Python 3.12 | CPython 3.12.16；离线套件 118 passed、1 live test deselected；行覆盖率 86.82% |
| 测试组成 | unit 79、integration 27、contract 12；另有 live 1。参数化用例计入 pytest 收集总数 |
| Ruff、编译、锁文件 | `ruff check .`、`compileall -q src tests`、`uv lock --check` 均通过 |
| MCP SDK | 已安装 MCP Python SDK 2.3.0；read-only/standard 各自经真实 stdio 子进程列工具、校验 schema 并调用 |
| 构建与 wheel | sdist 和 wheel 构建通过；干净 Python 3.11 环境安装成功；001/002 SQL 均打包；临时目录 `init` 与 `doctor` 通过 |
| 数据库诊断 | schema 2、`integrity_check=ok`、外键违规 0、FTS5 可用、数据目录 0700、数据库 0600 |
| 有界 live 冒烟 | 1 个冒烟用例通过，依次对 arXiv、HF Daily Papers、HF Models、GitHub Search 各请求一页；无生产数据库、无凭证依赖 |
| 特定 MCP 宿主 | 未验证；当前没有指定/配置目标宿主。SDK stdio 验证不能替代此项 |
| 远程 CI | 提交推送后以 GitHub Actions 实际运行状态补记；本地结果不冒充远程 CI |

完整离线命令：

```bash
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  uv run --locked pytest -p pytest_asyncio.plugin -p pytest_cov.plugin \
  -m 'not live' --cov=info_desk --cov-report=term-missing --cov-fail-under=80 -q
```

行覆盖率为 86.82%。开启分支测量时全包 combined coverage 为 83%；关键分支覆盖：`resolver.py` 91.7%、`storage/repository.py` 70.8%、`sync_engine.py` 69.7%。因此归并器达到约 90% 分支目标，Repository 和 Sync Engine 尚未达到；CI 当前以 80% 行覆盖门槛防止明显回退，并未把行覆盖冒充分支覆盖。

## A01–A12 验收矩阵

| 编号 | 结果与可复查证据 | 边界 |
|---|---|---|
| A01 | **通过**：`tests/unit/test_resolver.py::test_arxiv_and_hf_paper_with_same_id_share_one_item` | 通过规范化 arXiv ID 关联；不表示所有不同来源的语义身份均可自动判断 |
| A02 | **通过**：`test_one_way_weak_reference_does_not_merge`、`test_source_matching_multiple_items_stays_pending_without_third_item`、冲突事务/裁决用例；CLI `test_cli_reviews_and_attaches_pending_source` | 冲突新来源保存在 `pending_sources`，显式选中候选后单事务挂接；不自动合并两个已有 Item |
| A03 | **通过**：`tests/integration/test_repository.py::test_automatic_observation_is_idempotent_and_only_material_changes_update_item`；重复/恢复扫描在 `test_third_page_failure_retains_prior_waterline_and_rerun_recovers` | 保证重复已观察事实不会重复建源或事实事件；上游索引延迟仍受时间重叠策略边界约束 |
| A04 | **通过**：`test_third_page_failure_retains_prior_waterline_and_rerun_recovers`、`test_page_transaction_failure_rolls_back_records_and_statistics` | 第三页失败保留旧水位并重跑恢复；页内异常验证 SQLite 记录及统计一并回滚 |
| A05 | **通过**：`tests/integration/test_watch_refresh.py::test_old_watched_paper_detects_new_official_code_and_event` | 使用确定性适配器模拟旧论文的后续代码发现，不依赖隔日等待 |
| A06 | **通过**：`tests/unit/test_resolver.py::test_ignored_item_remains_deduplicated_and_keeps_manual_notes` | 验证忽略状态与人工备注在重抓后保留 |
| A07 | **通过（SDK/CLI）**：`tests/contract/test_mcp_stdio.py` 两 profile 子进程用例、`tests/contract/test_cli.py` 与 `test_cli_direct.py` | stdio stdout 无协议外文本；read-only 三工具、standard 六工具；目标宿主联调未验证 |
| A08 | **通过（已覆盖安全契约）**：`tests/unit/test_http_client.py`、`test_inspect.py`、`test_contracts.py`、`tests/contract/test_server_direct.py` | 覆盖白名单重定向、跨主机不转交凭证、体积上限、限速、输入约束和结构化错误；不构成完整安全审计 |
| A09 | **通过**：`tests/integration/test_service.py::test_service_events_and_status_allow_independent_consumer_cursors`、Repository 的事件快照/GitHub 重命名用例 | 两消费者独立游标；迁移时旧事件快照以迁移时仍存在的 Source 补建，无法恢复当年已变更的源字段 |
| A10 | **通过**：`tests/integration/test_storage_safety.py::test_fts5_and_like_fallback_search_english_and_chinese` | 当前 SQLite FTS5 可用；用受控关闭 FTS 分支验证 LIKE 中英文降级 |
| A11 | **通过**：v1→v2 重复迁移、WAL 在线备份/恢复、integrity/FK、临时 wheel `init`/`doctor` | wheel 检查的独立安装环境为 Python 3.11；Python 矩阵覆盖 3.11 与 3.12 的源码运行 |
| A12 | **通过**：`tests/integration/test_watch_refresh.py::test_old_watched_paper_detects_new_official_code_and_event` | 确定性模拟已知来源不在今日发现窗口仍被 Watch 复查；平台长期失效仍会让队列延后而非保证准时发现 |

## 缺口与运行限制

- **P1：** Repository 和 Sync Engine 的分支覆盖分别为 70.8% 与 69.7%，低于约 90% 的目标；已有事务、分页、水位线、恢复、限流等主路径及若干边界回归，但未执行到所有输入/状态组合。后续优先补充查询筛选、合并限制和同步错误分支测试。
- **P1：** 没有指定的 MCP 宿主环境，也没有跨日长期 systemd 任务运行记录；部署前仍需在真实宿主完成工具调用和错误反馈联调，并观察进程重启后的持续同步。
- **P2：** GitHub Search 即使细分时间窗后仍可能返回 `incomplete_results`；实现会保留 `partial` 并阻止水位线推进，但不能保证 GitHub 索引的全局完整性。重叠窗口只能降低延迟索引风险，不能覆盖任意长的上游索引延迟。
- **P2：** 本次 live 每源仅请求一页，验证连通、限流和解析的基本路径，不验证全量 API 覆盖、写入或长期配额表现。
- **P2：** v1 历史事件没有独立来源快照。v2 迁移只能从迁移时仍关联的 Source 回填，源记录在迁移前已被改名/移除的历史值无法复原。

以上结果只支持当前已测场景，不据此宣称已完成目标宿主验收或生产长期运行认证。
