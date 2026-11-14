# information-desk 智能体接口设计

> 本文是已实现 MCP/CLI 接口的契约基线。测试证据见[测试与发布验收](validation-results.md)，代码目录见[项目结构](project-structure.md)。

## 1. 工具注册与调用边界

同一个 MCP stdio 服务以 `info-desk serve --transport stdio --profile <profile>` 启动。默认使用 `read-only`。部署方在启动时选择模式，工具参数不能切换模式。

| 工具 | `read-only` | `standard` | 副作用 |
|---|---|---|---|
| `search_items` | 注册 | 注册 | 读取 SQLite |
| `list_events` | 注册 | 注册 | 读取 SQLite |
| `get_sync_status` | 注册 | 注册 | 读取 SQLite |
| `inspect_source` | | 注册 | 访问源站，产生请求与日志 |
| `save_item` | | 注册 | 更新审核字段；通过来源导入时会访问源站并写库 |
| `fetch_updates` | | 注册 | 访问源站，写入来源、实体、事件和运行记录 |

管理 CLI 提供历史回填、冲突裁决、迁移和备份恢复。定时同步通过 CLI 调用同一应用服务，使用持久化周期作业。服务启动时打开指定的数据目录及配置。只读模式以 SQLite 只读方式打开已初始化的库；初始化和升级由 CLI 执行。stdout 留给 MCP 协议，服务日志写 stderr 或日志文件。

工具描述须明确是否联网、是否写库以及何时调用。只读属性可作为 MCP 注解发布，工具注册与实际访问行为由服务端配置决定。[MCP 工具注解](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/)

## 2. 通用契约

### 2.1 请求和响应

- 工具按顶层命名参数调用，例如 `search_items(keyword="VLA", limit=20)`。输入拒绝未知字段，布尔、整数及字符串类型按 schema 校验。除专门说明外，省略可选字段使用默认值，显式 `null` 为无效输入。
- 时间输入为含偏移的 RFC 3339 时间，归一化为 UTC。所有区间采用 `[from, before)`；`first_seen_after` 和 `updated_after` 使用严格大于。输出使用 UTC `Z`；不可得的时间以 `null` 表示。
- `item_id`、`event_id` 和 `run_id` 为正整数。`revision` 为非负整数。标识在当前数据库内有效。
- 每个工具以带类型的响应模型发布输入及输出 schema。使用 Pydantic 约束字段、枚举与嵌套结构。业务失败走 `ok=false`；非法 MCP schema 输入由 SDK 返回协议工具错误，输入符合 schema 但违反业务约束时返回 `INVALID_ARGUMENT`。[MCP Python SDK 结构化输出](https://py.sdk.modelcontextprotocol.io/v2/servers/structured-output/)

成功响应示例：

```json
{
  "ok": true,
  "data": {"items": [], "next_cursor": null},
  "warnings": []
}
```

失败响应示例：

```json
{
  "ok": false,
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "条目已更新，请重新读取后再提交。",
    "retryable": false,
    "details": {"item_id": 42, "current_revision": 4}
  }
}
```

`message` 供展示，程序按 `code` 分支。可选 `retry_after_seconds` 只在可给出等待时间时返回。成功时 `warnings` 始终为数组，元素包含 `code`、`message`，可附 `feed`。刷新取得部分可用结果时使用 `ok=true` 和 `data.status="partial"`。响应和日志均不得暴露凭证，工具结果也不得带 Python traceback。

| 错误码 | 触发条件 | 调用方处理 |
|---|---|---|
| `INVALID_ARGUMENT` | 参数非法、空补丁、时间范围倒置 | 修正参数 |
| `INVALID_CURSOR` | 搜索游标损坏、版本或筛选条件不匹配 | 从第一页重新查询 |
| `INVALID_TARGET`、`UNSUPPORTED_SOURCE` | 来源标识非法或平台/路径类型未开放 | 修正目标 |
| `NOT_FOUND` | Item 或源站资源不存在 | 校对标识 |
| `UPSTREAM_FORBIDDEN` | 上游拒绝访问 | 检查源站可见性或部署凭证 |
| `UPSTREAM_TIMEOUT`、`UPSTREAM_UNAVAILABLE` | 上游暂时不可用 | 按重试提示处理 |
| `RATE_LIMITED` | 上游配额不足 | 按 `retry_after_seconds` 等待 |
| `SYNC_ALREADY_RUNNING` | 至少一个所选 feed 锁被占用且本次刷新未启动 | 查询同步状态 |
| `REVISION_CONFLICT` | 条目修订号与请求不一致 | 重读并重新判断变更 |
| `MERGE_CONFLICT` | 来源指向多个已存在实体 | 记录冲突，交由管理 CLI |
| `DB_BUSY`、`SCHEMA_MISMATCH` | 写锁等待耗尽或版本不匹配 | 前者有限重试，后者由管理员处理 |
| `INTERNAL_ERROR` | 未预期服务故障 | 查本地诊断日志 |

## 3. 公共输出对象

| 对象 | 字段 |
|---|---|
| `SourceRef` | `source_type`、`source_uid`、`external_id`、`url`、`published_at`、`source_updated_at`。稳定来源身份为 `(source_type, source_uid)` |
| `ItemView` | `item_id`、`revision`、`title`、`summary`、`category`、`triage_status`、`is_watched`、`code_status`、`first_seen_at`、`updated_at`、`sources`、`sources_truncated`。精确查询时另返回 `notes` |
| `EventView` | `id`、`item_id`、`event_type`、`created_at`、`source`（可空的来源快照）、`payload`（由事件类型决定） |

`source_type` 为 `arxiv | hf_paper | hf_model | github_repo`。`feed` 为 `arxiv | hf_papers | hf_models | github`。`triage_status` 为 `inbox | saved | ignored`。`code_status` 为 `unknown | not_detected | detected`。

列表最多返回每项 5 个来源，精确查询最多 50 个。优先放与本次来源筛选匹配的来源，其余按来源类型和稳定 ID 排序。返回摘要预览最多 800 字符。若来源超出上限，置 `sources_truncated=true`。用户备注最多 4,000 字符。所有内容长度以 Unicode 字符计。

事件 `payload` 定义如下。源 URL、外部 ID 等可引用信息取事件发生时的快照。

| `event_type` | `payload` |
|---|---|
| `discovered` | `title` |
| `source_attached` | `relation_type`、`evidence_level` |
| `content_changed` | `changed_fields` 字符串数组 |
| `code_detected` | `previous_status`、`current_status="detected"`、`signals` |

## 4. 工具定义

### 4.1 `search_items` · 查询实体

| 参数 | 类型及默认值 | 约束与语义 |
|---|---|---|
| `item_id` | 正整数，可选 | 精确查询。仅可单独传入，返回一条或 `NOT_FOUND`，可查任意审核状态 |
| `keyword` | 字符串，可选 | 1–200 字符，查询标题、摘要、笔记，按普通文本处理 |
| `category` | 字符串，可选 | 1–64 字符，匹配已存分类 |
| `triage_status` | 枚举，可选 | 缺省排除 `ignored`；明确传入 `ignored` 可查忽略项 |
| `is_watched` | 布尔，可选 | 按是否关注过滤 |
| `code_status` | 枚举，可选 | 按源码信号状态过滤 |
| `source_type` | 来源类型，可选 | 按关联来源过滤 |
| `published_from`、`published_before` | 时间，可选 | 至少一条来源满足发布日期区间，与 `source_type` 约束同一条 Source。未知发布日期不匹配 |
| `first_seen_after`、`updated_after` | 时间，可选 | 按 Item 首次入库和最近实质变更时间过滤 |
| `limit` | 整数，默认 20 | 1–100 |
| `cursor` | 不透明字符串，可选 | 使用上一页的 `next_cursor` |

返回 `data.items: list[ItemView]`、`data.next_cursor: str | null`。按 `(updated_at DESC, item_id DESC)` 键集分页，游标包含版本、筛选摘要和排序边界。数据库在翻页期间可以变化，列表用于交互检索，持续追踪使用事件接口。FTS5 可用时通过转义后的词项检索，否则用参数化 LIKE，并带 `SEARCH_FALLBACK` 告警。结果为空返回空数组。

### 4.2 `list_events` · 读取全局事件流

| 参数 | 类型及默认值 | 约束与语义 |
|---|---|---|
| `after_id` | 非负整数，默认 0 | 排他下界 |
| `limit` | 整数，默认 50 | 1–100 |
| `event_types` | 事件类型数组，可选 | 1–4 个唯一值 |
| `include_ignored` | 布尔，默认 false | 是否包含当前已忽略实体 |
| `only_watched` | 布尔，默认 false | 是否只返回当前受关注实体 |

返回 `events: list[EventView]`、`next_after_id: int`、`has_more: bool`。按 ID 升序，以匹配条件多取一条计算 `has_more`。`next_after_id` 等于最后实际返回的 ID，空页返回请求的 `after_id`。筛选按查询时的审核与关注状态生效。消费者改变筛选范围时，按业务所需使用对应游标或从更早的 ID 重读。需要回溯曾被审核状态排除的事件时，从旧游标重新读取。处理成功后由消费者保存游标，重试可按事件 ID 去重。

### 4.3 `get_sync_status` · 检查数据覆盖

可选 `feeds` 为 1–4 个唯一采集子源。缺省返回全部启用子源。响应含 `jobs`、`watch_refresh`。

- `jobs[]` 包含 `job_key`、`feed`、`query`、`coverage_from`、`completed_until`、`last_attempt_at`、`last_success_at`、`last_status` 和 `last_error_code`。每个周期查询单独报告。启用但未运行的作业返回 `last_status="never"`，覆盖字段为 `null`。状态枚举为 `never | running | success | partial | failed`；`running` 从当前作业运行记录判断，恢复时清理已失去锁的陈旧记录。
- `watch_refresh` 返回 `last_attempt_at`、`last_status`、`checked`、`failed`、`pending_due`。它是独立队列，统计覆盖全部来源，计数在首次运行前可为 `null`。
- 手动扫描的覆盖由该次 `fetch_updates` 回执返回。调用状态工具时只读取本地记录。

### 4.4 `inspect_source` · 探查来源

输入 `target: str`，1–2,048 字符。接受裸 arXiv ID、arXiv 论文 URL、HF Paper/Model URL、GitHub 仓库 URL。解析受支持的官方 HTTPS 主机和路径后，由适配器构造请求目标。URL、重定向和凭证管理遵循[架构基线第 11 章](../information-desk-architecture-v1.1.md#11-异常并发数据安全与资源限制)。

返回 `source: SourceRef`、`title`、`summary`、`checked_at`、`code_status`、`signals`、`root_files`、`checked_depth`、`extracted_links`、`content_excerpt`、`limitations`。其中：

- `content_excerpt` 为可空对象，字段为 `text`（最多 4,000 字符）、`source_url`、`truncated`、`trust="untrusted_external"`。工具保留信号和出处，正文仅作引用资料。
- `extracted_links` 最多 30 条，每条给出 `url`、`relation_type`、`evidence_level`。链接是已观察证据；详情请求由适配器的显式规则控制。
- GitHub 根目录信号用于判断 `detected`。详情可用、目录检查失败时返回 `unknown` 和告警。探查仅使用远端元数据和有界文本，服务不执行外部代码，也不更新实体。

### 4.5 `save_item` · 更新审核或导入来源

| 参数 | 类型及约束 |
|---|---|
| `item_id` | 已有条目的正整数 ID，与 `source` 二选一 |
| `source` | 形如 `{"target":"https://arxiv.org/abs/2611.01234"}`，复用探查目标校验 |
| `expected_revision` | 更新已有条目时必填，来源导入分支禁止传入 |
| `triage_status` | 可选，`inbox | saved | ignored` |
| `category` | 可选，去首尾空白后 1–64 字符 |
| `is_watched` | 可选布尔 |
| `notes` | 可选字符串，最多 4,000 字符，空串清除备注 |

- 按 ID 更新时须提供至少一个审核字段。省略的字段保留原值。校验修订号、修改字段及递增修订号在同一事务内执行。无实际字段变化返回 `unchanged`，保留修订号。
- 来源导入先向平台验证身份。新 Item 缺省为 `saved`，可同时设置审核字段。若来源已存在，且调用未要求改变审核字段，返回该 Item 和 `unchanged`。如果导入命中已有 Item 并要求修改其审核字段，返回 `REVISION_CONFLICT` 及当前 ID 与修订号，调用方可改用按 ID 更新。多目标归属返回 `MERGE_CONFLICT` 并持久化冲突。
- 应答丢失后，按 ID 更新可先重读当前字段确认是否已经生效。来源导入按稳定身份去重。来源事实事件与导入写入原子提交，人工审核本身不产生采集事件。

返回 `item_id`、`revision`、`triage_status` 和 `action: created | updated | unchanged`。`action` 按 Item 计算，已有 Item 成功挂接新 Source 时返回 `updated`。

### 4.6 `fetch_updates` · 请求一次有界刷新

| 参数 | 类型及默认值 | 约束与语义 |
|---|---|---|
| `days` | 整数，默认 2 | 1–14，决定此次回溯区间 |
| `feeds` | 数组，可选 | 1–4 个唯一且启用的 feed；缺省使用启用列表 |
| `topics` | 字符串数组，可选 | 1–10 个非空主题，每项最多 100 字符；缺省使用配置 |
| `limit` | 整数，默认 30 | 1–100，仅限制本次回执中的事件预览 |

请求开始时固定 `[now - days, now)`，再按各平台查询粒度切段。手动刷新使用独立作业命名空间，与定时作业共用按 feed 的同步锁和速率限制；一次刷新按固定顺序尝试锁定所有所选 feed，获取失败即释放已获得的锁。本工具只执行发现扫描，定时同步负责 `watch_refresh`。MCP 刷新默认总预算 30 秒，可由部署配置调整并应小于目标宿主工具超时。预算到期结束当前扫描，已提交页面和事件可继续读取。宿主断开后不启动新的页面请求，既有事务由服务完成或回滚。

返回数据字段：

| 字段 | 含义 |
|---|---|
| `run_id`、`started_at`、`finished_at` | 本次请求及运行时间 |
| `status` | 所有子作业完整时为 `success`；存在已提交页面或完成区间且仍有未完成范围时为 `partial`；运行已建立但没有可用结果时为 `failed` |
| `jobs` | 每个规范化查询作业的 `job_key`、`feed`、`window_start`、`window_end`、`completed_until`、`status`、`error_code`。单个 feed 可有多个查询作业 |
| `stats` | `fetched`、`new_items`、`new_sources`、`new_events`、`conflicts` |
| `events_preview`、`events_preview_truncated` | 本次运行产生的事件预览，按 ID 升序；重复抓取命中既有事件时不算本次新事件 |

刷新回执不携带全局续读游标。消费方使用 `list_events` 读取完整事件流。未完整枚举的时间段保留原进度；空结果只有在对应区间完整扫描后才表示该次查询无发现。运行记录建立前的参数、权限或锁错误通过通用失败响应返回。

以下回执表示 arXiv 完成、GitHub 超时且未完成首个区间。两项均未产生新事件。

```json
{
  "ok": true,
  "data": {
    "run_id": 120,
    "started_at": "2026-11-13T15:00:00Z",
    "finished_at": "2026-11-13T15:00:30Z",
    "status": "partial",
    "jobs": [
      {
        "job_key": "example-arxiv-job",
        "feed": "arxiv",
        "window_start": "2026-11-11T15:00:00Z",
        "window_end": "2026-11-13T15:00:00Z",
        "completed_until": "2026-11-13T15:00:00Z",
        "status": "success",
        "error_code": null
      },
      {
        "job_key": "example-github-job",
        "feed": "github",
        "window_start": "2026-11-11T15:00:00Z",
        "window_end": "2026-11-13T15:00:00Z",
        "completed_until": null,
        "status": "failed",
        "error_code": "UPSTREAM_TIMEOUT"
      }
    ],
    "stats": {"fetched": 0, "new_items": 0, "new_sources": 0, "new_events": 0, "conflicts": 0},
    "events_preview": [],
    "events_preview_truncated": false
  },
  "warnings": [{"code": "UPSTREAM_TIMEOUT", "message": "GitHub 扫描尚未完成。", "feed": "github"}]
}
```

## 5. 智能体调用示例

| 用户意图 | 调用顺序 |
|---|---|
| 过去两天发布了哪些 VLA 论文 | `get_sync_status` → 按发布日期、主题及论文来源类型调用 `search_items`。必要时分别查询 arXiv 和 HF Paper 并按 Item ID 去重 |
| 上次日报之后有哪些项目发布代码 | 用消费方持久化的游标调用 `list_events`，关注 `source_attached` 和 `code_detected`；按 Item ID 查询详情，处理成功后持久化游标 |
| 收藏这篇论文，后续持续跟踪 | 查当前 Item 与修订号，再用 `save_item` 设置 `triage_status="saved"`、`is_watched=true`。库里没有条目时走来源导入 |
| 立即查有没有新发现 | `fetch_updates` 后检查各查询作业的覆盖，再查询本地 Item 或读取事件；部分成功时说明哪些来源尚未扫完 |

契约测试应验证实际 stdio 工具列表和 schema、默认工具范围、空结果、分页、版本冲突和部分刷新。至少用一个目标智能体宿主测试查找、更新和错误反馈。实施阶段见[项目结构](project-structure.md#5-实施顺序与验收)。

实现使用 MCP Python SDK 2.x 的类型化输入/输出模型，参数 schema 约束长度、范围及枚举。read-only profile 通过只读 SQLite 连接注册恰好三个本地工具；standard 注册全部六个工具。SDK stdio 子进程测试不等同于特定宿主互操作验收，目标宿主仍需单独验证。
