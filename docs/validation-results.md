# M5 工程测试与发布验收

验收记录日期：2026-11-13（America/Los_Angeles）。本报告只记录本轮实际执行的结果。SDK stdio 子进程不代表连接过特定智能体宿主；隔离 systemd 测试不代表跨日长期运行。

## 测试与构建结果

| 检查 | 实际结果 |
|---|---|
| 离线测试总数 | 146 passed，0 failed，1 live test deselected；unit 83、integration 51、MCP/CLI contract 12 |
| Python 3.11 | CPython 3.11.18；完整离线套件 146 passed、1 deselected |
| Python 3.12 | CPython 3.12.16；完整离线套件 146 passed、1 deselected |
| 行覆盖率 | 88.20%（高于前轮基线 86.82%；覆盖率不是行为正确性的替代指标） |
| 关键分支覆盖率 | Resolver 91.9%（57/62）；Repository 78.2%（136/174）；Sync Engine 87.5%（84/96）；Watch 71.7%（33/46） |
| Ruff、编译、锁文件 | `ruff check .`、`compileall -q src tests`、`uv lock --check` 和 `git diff --check` 均通过 |
| wheel 构建与安装 | sdist/wheel 构建通过；wheel 安装在干净 Python 3.11 venv；001/002/003 SQL 均从包资源读取；临时库 `init`、`doctor` (schema 3) 通过 |
| MCP SDK 契约 | 安装的 MCP Python SDK 2.3.0；read-only / standard 各自以真实 stdio 子进程验证工具注册、schema、调用、结构化业务错误和 stdout 协议边界 |
| 数据库安全 | v1 通过顺序迁移至 schema v3，重复初始化通过；保留现有 Item/Source/Event/Link/Run/Sync ID；`integrity_check=ok`、外键违规 0；WAL 在线备份恢复通过 |
| 搜索 | SQLite FTS5 与受控关闭 FTS 后的 LIKE 降级均通过；中英文检索通过 |
| 有界 live 冒烟 | `INFO_DESK_LIVE=1 ... pytest -m live`：1 passed、146 deselected；对 arXiv、HF Papers、HF Models、GitHub 各进行一个只读请求，不打开数据库、不需要 Token |
| systemd | `systemd-analyze verify` 检查 service/timer；用户级 manager 通过两个隔离 transient unit 实际运行 `init` 和 `doctor`。未启用用户 timer，也没有跨日运行观察 |
| 指定 MCP 宿主 | 未验证：本轮没有配置目标宿主；SDK stdio 测试不能替代宿主联调 |
| GitHub Actions | [Push CI run 43572595908](https://github.com/Tuzfucius/information-desk/actions/runs/43572595908)：**success**；静态检查、Python 3.11/3.12 离线测试和 wheel 安装均通过。Live job 按配置仅由定时或手动触发，本次 push 未运行；本机有界 live 冒烟通过 |

离线覆盖率执行命令：

```bash
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  uv run --locked pytest -p pytest_asyncio.plugin -p pytest_cov.plugin \
  -m 'not live' --cov=info_desk --cov-report=term-missing \
  --cov-fail-under=80 -q
```

本机 Python 3.11 验收使用独立 uv 环境。该工作区 pytest 插件自动发现会加载系统 ROS 插件，因此显式禁用全局自动发现并加载项目锁定的 pytest-asyncio/pytest-cov 插件；这不会跳过项目插件。

## M5 可靠性与扩展测试证据

| 能力 | 可复查测试 | 结果 |
|---|---|---|
| 超过每窗口页数上限后持续推进 | `test_twenty_five_pages_are_recovered_by_gapless_window_splitting`，配置上限 20、数据 25 页 | 25 条 Source/Event 入库，子窗口无间隙完成 |
| 稳定游标失败恢复及后续增量 | `test_fake_cursor_source_uses_registry_and_recovers_durable_cursor`、`test_durable_cursor_advances_without_exceeding_per_run_page_cap` | 429 后从恢复 Token 续读；成功 checkpoint 独立持久化；下一轮新增记录从 cursor 继续；不报告时间水位线 |
| 不完整 cursor 契约 | `test_cursor_source_without_checkpoint_stays_partial` | 缺少成功 checkpoint 时结果为 partial，cursor 和时间覆盖均不伪推进 |
| checkpoint 与 segment 原子性 | `test_provider_cursor_and_segment_completion_share_one_transaction` | SQLite 注入提交故障后两项均回滚 |
| 完成子段在兄弟失败时保留 | `test_completed_sibling_segment_survives_another_segment_failure_and_recovers` | 已完成前缀保留，失败子段重跑后完整恢复 |
| 页面提交后进程退出 | `test_page_commit_before_segment_ack_is_idempotent_after_restart` | 重启重取后只有一个 Item/Event |
| GitHub Search 不完整 | `test_incomplete_search_response_never_claims_completion`、`test_one_thousand_result_search_cap_is_partial`、`test_incomplete_github_window_refines_by_subday_ranges`、`test_unresolvable_github_window_remains_partial` | 可细分窗口继续；无法证明完整时保留 partial，不推进覆盖 |
| Watch 公平与积压消化 | `test_watch_queue_is_bounded_and_drains_fairly_across_runs[100/500]` | 100/500 个来源分多轮完成；每轮最多处理 100 个，来源不重复占位 |
| Watch 失败退避与恢复 | `test_watch_rate_limit_is_bounded_by_failure_policy_then_recovers`、`test_watch_http_429_preserves_retry_after_error_classification` | 限流保留错误分类，Retry-After 受上限约束；来源可在退避后恢复 |
| Watch 取消与不支持能力 | `test_source_unwatched_before_due_query_is_not_refreshed`、`test_watch_defers_sources_without_inspection_support` | 取消关注不再请求；不支持探查的来源延期而不记为源站失败 |
| Watch Adapter 移除、Feed 改名与状态可观测性 | `test_watch_defers_sources_after_their_adapter_is_removed`、`test_watch_routes_sources_after_feed_rename`、`test_service_events_and_status_allow_independent_consumer_cursors` | 已移除 Adapter 的 Watch 延期并计入 skipped；Feed 改名后可按 Source type 回退路由并在下次更新持久化新 Feed；默认状态包含禁用及历史 Feed Job |
| 展示字段稳定 | `test_ten_alternating_source_updates_keep_canonical_fields_deterministic`、`test_manual_locks_protect_fields_while_source_facts_and_events_change`、`test_merge_reselects_canonical_source_when_winner_is_lower_priority` | 十轮 arXiv/HF/GitHub 来源交替更新不抖动；合并后重选权威 Source；人工锁字段优先，来源事实变化仍可事件化 |
| 第五来源隔离 | `tests/integration/test_m5_source_registry.py` 的 Fake cursor-only `test_notice` adapter，及 undeclared Source type 拒绝测试 | 新 Feed 通过注册表、通用 Repository/Resolver/Event/Search 集成，无需改 MCP/数据库核心代码；不合契约的数据不会写库 |

## V1.1 A01–A12 验收矩阵

| 编号 | 状态与证据 | 边界 |
|---|---|---|
| A01 | **通过**：`test_arxiv_and_hf_paper_with_same_id_share_one_item` | 证明规范化 arXiv ID 归并；不宣称所有来源语义身份都能自动判断 |
| A02 | **通过**：`test_one_way_weak_reference_does_not_merge`、`test_source_matching_multiple_items_stays_pending_without_third_item`、冲突裁决和 CLI 测试 | 不自动合并两个已有 Item；冲突来源保留待处理证据，人工选目标后事务挂接 |
| A03 | **通过**：`test_automatic_observation_is_idempotent_and_only_material_changes_update_item`、Fake cursor 重启/增量测试 | 重抓不重复实体/事件；窗口覆盖仍受上游索引延迟边界约束 |
| A04 | **通过**：第三页故障恢复、页内事务回滚、段级失败恢复、预算耗尽回归测试 | 故障不推进未完成区间；预算未执行的 Job 明确 skipped |
| A05 | **通过**：`test_old_watched_paper_detects_new_official_code_and_event` | 使用确定性适配器验证后续代码发现，不依赖隔日等待 |
| A06 | **通过**：`test_ignored_item_remains_deduplicated_and_keeps_manual_notes`、canonical 字段锁定测试 | 重抓保护人工状态、备注及锁定的标题/摘要 |
| A07 | **通过（CLI/SDK）**：12 项 CLI/MCP 契约测试，包含两个真实 stdio profile 子进程 | 指定终端 MCP 宿主未配置，不能报告宿主联调通过 |
| A08 | **通过（已覆盖安全契约）**：`test_http_client.py`、`test_inspect.py`、`test_contracts.py` | 覆盖域名/重定向白名单、跨主机凭证不转交、响应体上限、限流和结构化错误；不构成全量安全审计 |
| A09 | **通过**：独立事件消费者游标测试、GitHub 改名后的事件来源快照测试 | v2 迁移前已经丢失的历史源字段不能追溯恢复 |
| A10 | **通过**：`test_fts5_and_like_fallback_search_english_and_chinese` | 在当前 SQLite 与强制回退路径验证英文、中文查询 |
| A11 | **通过**：v1→v2→v3 顺序迁移、重复初始化、WAL 备份/恢复、完整性检查、wheel `init`/`doctor` | schema 采用增量升级，未对真实用户库运行破坏性测试 |
| A12 | **通过**：`test_old_watched_paper_detects_new_official_code_and_event`，以及 Watch 分批队列测试 | 证明到期 Watch 有界执行和结果事件化，不表示平台永久可用或严格准时发现 |

## 剩余限制与风险

### P1

- Repository 分支覆盖 78.2%，低于建议的 85–90%；未执行分支主要分布在查询过滤组合、来源字段变体、合并冲突附加状态和迁移回滚边界。事务、迁移、游标 checkpoint 原子性、覆盖前缀和备份恢复已有行为测试；后续应按真实故障路径继续补测，不应为覆盖率加入无行为意义断言。
- Sync Engine 分支覆盖 87.5%；重点恢复、分段、游标和预算分支已有行为测试，剩余分支按来源错误语义继续补充。
- Watch 分支覆盖 71.7%；队列批量、公平消费、退避、限流恢复、取消关注、不支持能力、适配器移除及 Feed 改名已实测；未覆盖的 linked-source 组合与少见异常仍需后续按来源契约补充。
- 未配置目标 MCP 宿主；未启用实际用户级 timer，也没有跨日或生产数据运行记录。
- 当前 Watch 的“论文发现官方 GitHub 实现”后续探查仍是 HF Paper→GitHub 的领域策略。新增普通来源的增量同步、Source 检查和 Watch 周期走通用契约；若新来源引入新的跨平台主动追踪关系，需在后续按实际关系策略泛化该钩子。

### P2

- GitHub Search 的 1,000 条上限、`incomplete_results` 与索引延迟是源站完整性边界。代码会细分可证明的时间窗并保留无法确认的 partial，但无法保证 GitHub 全局搜索完整。
- 限流器在 SafeHttpClient 进程内共享；多个独立进程不共享请求配额桶。文件锁保护同源同步作业，不协调 MCP `inspect_source` 与跨进程平台全局额度。
- 本次 live 每个公开源仅请求一页，用于确认连通和基础解析，不证明全量覆盖或长周期配额表现。没有接入真实新来源；FakeSourceAdapter 仅证明扩展契约。
- systemd 只执行了临时目录下的瞬时启动/诊断；Timer 安装、停启恢复和跨日服务状态仍需部署者在目标主机验证。

以上结果支持当前离线、有限 live 和隔离部署测试范围，不代表目标 MCP 宿主验收或生产长期运行认证。
