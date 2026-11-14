# 新增 Source Adapter

本指南说明如何新增信息源而不改同步引擎、Resolver、SQLite Repository 或 MCP 工具。当前适配器由显式 `AdapterRegistry` 组装；不使用自动发现或动态导入。第五来源的可运行样例位于 [`tests/integration/test_m5_source_registry.py`](../tests/integration/test_m5_source_registry.py)。

## 1. 领域边界

- **Feed**：一个可调度采集入口，例如 `openreview` 或 `team_rss`。
- **Source**：平台上的稳定来源实体，例如某个讨论、帖子或模型仓库。
- **Item**：information-desk 内部归并后的技术实体。Feed 可创建技术资讯 Item，不要求它是论文。
- **SourceLink**：Source 之间有证据等级的关系。只有强证据才参与自动归并；弱引用仅保存关系。
- **Event**：新增、关联、实质内容变化或代码发现等可追溯事实。
- **SyncJob**：Feed 加规范化查询形成的扫描作业，维护窗口和可恢复进度。

来源特有字段放在 Source 的 `metadata`；稳定身份、标题、摘要、发布日期、更新时间、URL 和关系仍使用结构化字段。新增来源类型及 Feed 名称已由 schema v3 的受约束文本标识符支持。新增现有事件模型之外的关系或事件语义前，先评估是否需要增量迁移；不要把关系表换成万能 JSON。

## 2. Adapter 职责与能力声明

适配器负责：规范化查询、构造受控请求、解析平台响应、分页、返回 `CandidateSource`、标注是否完整，以及对已知实体执行 `inspect`。网络访问使用注入的 `SafeHttpClient` 和 `bounded_get`；不要在适配器中创建绕过白名单、限流、响应体上限的第二条 HTTP 通道。

`SourceCapabilities` 至少声明 Source 类型、发现模式、完整扫描能力和 Watch 策略：

```python
SourceCapabilities(
    source_types=("release_notice",),
    discovery_mode="cursor",       # window | cursor | poll | none
    window_resolution_seconds=None,
    window_filtering="none",        # server | client | none
    durable_cursor=True,
    supports_inspect=True,
    target_matcher=matches_release_url,
    request_hosts=("releases.example.org",),
    min_request_interval_seconds=1.0,
    watch_interval_hours=6,
    watch_timeout_seconds=15,
    title_priorities={"release_notice": 20},
    query_builder=lambda topics: [{"topic": topic} for topic in topics],
)
```

选择扫描策略：

| 平台保证 | 声明方式 | 恢复行为 |
|---|---|---|
| 可以按服务器时间条件完整枚举 | `discovery_mode="window"`、`window_filtering="server"`、声明最小窗口粒度 | 达到页数上限或平台不完整时，按无间隙子窗口继续；每个完整子段独立持久化 |
| 有跨请求稳定的 continuation token | `discovery_mode="cursor"`、`durable_cursor=True`；分页继续时返回 `PageResult(resume_safe=True)`，完整批次最后一页另返回 `checkpoint_token` | 暂时失败保存未完成段的 `page_token`；完整批次的 checkpoint 写入 `sync_state.provider_cursor`，下次增量扫描从该位置开始 |
| 仅能轮询有限的最近状态 | `discovery_mode="poll"`，使用适配器自身可证明的分页/更新策略 | 不得把有限轮询结果伪报为时间窗口完整覆盖；不可证明完整就返回 `partial` |
| 只有详情探查，没有列表发现 | `discovery_mode="none"` | 同步作业明确返回 `skipped / DISCOVERY_UNSUPPORTED`，仍可通过 `inspect_source` 使用 |

只有 `discovery_mode="window"` 且声明 `window_filtering="server"` 或 `"client"` 的来源会维护时间覆盖水位线。cursor 和 poll 的运行可以成功，但 `coverage_from` / `completed_until` 保持为空；这两个字段不表示轮询成功时间。`skipped` Job 与错误原因会持久化，查询状态不会把它降级为从未运行。

不能把页码或动态排序 offset 声称为稳定游标。`incomplete_results`、1000 条搜索上限、缺少 continuation token、超时和页数限制是不同失败原因；适配器以 `AdapterError`、`PageResult.warnings` 和 `source_response_incomplete` 表达。无法拆分且无法安全续游标时，状态必须保持 `partial`，记录可诊断原因。

`next_page_token` 表示当前批次尚有后续页；`checkpoint_token` 表示本批完整消费后下次增量扫描应使用的位置，两者不能混用。checkpoint 与 segment 完成状态在一个 SQLite 事务提交。声明 durable cursor 的适配器若完整响应没有提供 checkpoint，会保持 `partial` (`CURSOR_CHECKPOINT_MISSING`)。游标型 Job 不报告伪造的时间水位线，opaque token 也不会暴露在状态接口。

## 3. 稳定身份、字段和关系证据

`source_uid` 应优先选择平台永久 ID。若平台只有 URL，先规范化 scheme、host、路径大小写规则、尾斜线和可忽略查询参数，并单独测试重命名、别名与 URL 变体。`external_id` 可用于展示，可能会改名；身份约束由 `(source_type, source_uid)` 承担。

不要让采集顺序决定 Item 标题。能力声明中的 `title_priorities` 参与确定性 canonical Source 选择；更高优先级胜出，平级按稳定类型和 Source ID 排序。手工锁定字段始终优先。平台字段变化仍可产生 `content_changed` 事件，即使 canonical 展示字段没有变化。

每条 `extracted_links` 关系都应提供 `relation_type`、`evidence_level`、`evidence_origin`，有条件时记录短 `evidence_excerpt`。`strong` 只能用于可验证的共同身份或双向官方关系。一个来源若强关联多个已有 Item，会进入待处理冲突，适配器不能直接决定合并已有 Item。

## 4. 实现、注册和检查

新增适配器模块并通过 `AdapterRegistration(feed, adapter, capabilities)` 显式注册。Feed 和 Source type 名称均须符合 `^[a-z][a-z0-9_]{0,63}$`；Registry 会拒绝未声明的 Candidate Source type，SQLite 也会约束写入标识符。每种 Source 类型应由一个注册入口负责探查路由。查询构造器返回可稳定序列化的字典列表，避免运行时顺序或随机值改变 `job_key`。

```python
class FakeSourceAdapter:
    capabilities = SourceCapabilities(
        source_types=("test_notice",),
        discovery_mode="cursor",
        window_resolution_seconds=None,
        window_filtering="none",
        durable_cursor=True,
        supports_inspect=False,
        watch_interval_hours=3,
        title_priorities={"test_notice": 10},
        query_builder=lambda topics: [{"topic": topic} for topic in topics],
    )

    async def discover(self, query, window, page_token=None) -> PageResult:
        # Return next_page_token for another page and checkpoint_token at batch end.
        ...
```

把注册传给 `InformationService`/`SyncEngine` 的 AdapterRegistry。新增一个 Feed 不需要改 SyncEngine 的 feed 分支；如果新 Feed 需要新域名，将其 HTTPS 主机和最小请求间隔写入 capabilities，应用组装会把策略注册到 SafeHttpClient。所有重定向仍经过相同的 host allowlist。

Watch 可使用 `watch_interval_hours` 与 `watch_timeout_seconds`，全局 `watch_refresh_interval_hours`（若设置）覆盖所有适配器。无详情探查能力的 Source 被显式跳过并延期，不记为源站失败。Watch 失败使用配置的指数退避上限。

## 5. Fixture、契约和恢复测试

至少提供：

1. 脱敏的正常、空响应、边界分页和不完整响应 fixture。
2. Adapter 单测：参数规范化、稳定 Source ID、分页 token 校验、错误分类、限流头和字段归一化。
3. 通用契约测试：Source type、`CandidateSource`、`PageResult` 和错误分类可被核心层消费。
4. 集成测试：用临时 SQLite 执行发现、事件生成、重复运行、游标/窗口恢复、故障后重试和查询。
5. 扩展测试：只新增注册，不改 Resolver、Repository 或 MCP 工具；检查新类型事件快照和重启行为。
6. live 测试单独标记，不写生产数据库、不作为普通 PR 必需项。

第五来源的 Fake cursor 示例覆盖暂时 429 后跨运行恢复，也验证 `max_pages_per_window` 不被同一轮循环绕过。用 `uv run pytest -m 'not live'` 执行离线套件；通过 Ruff、编译、迁移、轮子安装和 MCP stdio 契约后，再提交新来源。

该 Fake 还在一轮完整完成后返回 provider checkpoint，随后增加一条记录并验证下一轮从已保存 cursor 开始；它不报告虚构的时间覆盖值。

## 6. 数据库迁移

新增 Feed 和新的 `source_type` 不需要新增表。若需要新增可查询核心字段、关系类型、Event 类型或新的约束，应新增编号 SQL 迁移并提高 `SCHEMA_VERSION`。迁移必须从已有 v1/v2 数据库顺序升级，保留 Item、Source、SourceLink、Event、SyncJob ID 和外键；必须测试重复初始化、回滚、备份恢复和外键检查。禁止删除用户库来解决迁移问题。

## 7. 完成验收

- `uv lock --check`、Ruff、编译和离线测试通过。
- 新适配器不允许任意 URL、未登记域名、无限响应体或无限重试。
- 重复同步不重复产生实体和事实事件；分页中断恢复不漏掉已确认数据。
- 不完整窗口不会推进水位线；已完成连续区间在另一子段失败时仍可观测。
- `info-desk status` 能显示该 Feed 的状态、错误、完成水位和待处理分段。
- `info-desk doctor`、wheel 安装、迁移/备份、read-only 与 standard MCP 契约通过。
- README 与运维文档记录 live、目标宿主、systemd 的真实验证边界。
