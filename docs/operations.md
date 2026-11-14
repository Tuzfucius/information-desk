# 本地部署与运行

## 环境准备

本工作区默认复用本机 Conda 的 `mujoco` Python；它只是本地选择，不是运行依赖。部署可使用任意受支持的 Python 3.11 或 3.12，由 `uv.lock` 重建依赖环境。

```bash
mujoco_python="$(conda run -n mujoco python -c 'import sys; print(sys.executable)')"
uv sync --locked --python "$mujoco_python"
uv run info-desk init
uv run info-desk doctor
```

无 Conda 的机器可使用系统 Python：

```bash
uv sync --locked --python 3.12
uv run info-desk init
uv run info-desk doctor
```

`doctor` 检查数据库迁移版本、SQLite 完整性和外键、数据目录与数据库权限、运行锁目录权限以及配置。若从项目根目录执行，也会报告 `uv.lock` 是否存在；独立安装的 wheel 没有项目锁文件时此项是提示，不影响运行。当前 schema 为 v3；v1/v2 数据库会在初始化时以增量事务迁移，不需要删除或重建用户数据库。

如果已有 `~/.information-desk/config.toml`，可以从仓库根目录的 `config.example.toml` 复制并调整。设置 `INFO_DESK_HOME` 后该路径优先于 TOML 的 `data.home`。相对的 `data.database` 路径相对于数据目录解析。

凭证通过当前进程环境变量传入。公开资源无需凭证，配置 GitHub Token 可提高查询额度。日志不得写入 Token。

```bash
export GITHUB_TOKEN="..."
export HF_TOKEN="..."
```

## MCP 宿主

先执行 `uv sync --locked` 和 `info-desk init`。宿主启动命令使用项目环境中的入口路径，并设置数据目录：

```json
{
  "mcpServers": {
    "information-desk": {
      "command": "/absolute/path/to/information-desk/.venv/bin/info-desk",
      "args": ["serve", "--transport", "stdio", "--profile", "read-only"],
      "env": {
        "INFO_DESK_HOME": "/home/username/.information-desk"
      }
    }
  }
}
```

`read-only` 提供本地检索、事件读取与状态查询。`standard` 还提供来源探查、人工整理和联网刷新。MCP stdio 的 stdout 专用于协议消息。诊断和错误日志写入 stderr 或轮转文件。

## 手动运行

```bash
uv run info-desk sync
uv run info-desk status
uv run info-desk search --keyword "Diffusion Policy"
uv run info-desk events --after-id 0 --limit 50
uv run info-desk inspect https://github.com/owner/repo
uv run info-desk backup ~/.information-desk/backups/intel-$(date -u +%Y%m%dT%H%M%SZ).db
uv run info-desk conflicts list
uv run info-desk conflicts resolve 12 --winner 42
```

搜索默认排除 `ignored`。按 Item ID 查询以及冲突处理由 MCP 工具或后续管理命令提供。同步按页写入来源和事件。只在数据区间完整枚举后推进水位线；服务器时间查询可以继续拆分，已确认完成的连续前缀会保留，稳定游标按适配器声明跨运行恢复，动态 offset 在失败后幂等重扫。

`info-desk status` 将发现同步与 Watch 队列分开显示。发现 Job 包含 `last_status`、`last_error_code`、最后成功覆盖位置和待处理分段数。Watch 统计到期来源、失败来源、下一到期时间和检查数。`success`、`partial`、`failed`、`skipped` 分别表示完成、仍有未完成工作、没有可用结果和预算/能力导致未检查；不要将 `partial` 当成成功覆盖。

Watch 每轮最多处理 `watch_refresh_limit_per_run` 个到期 Source，每个来源最多占用 `watch_item_budget_sec` 或适配器声明的更小预算。成功后按适配器默认周期下次检查；设置 `watch_refresh_interval_hours` 会覆盖全局周期。失败使用 `watch_failure_base_hours` 起步并以 `watch_failure_max_hours` 封顶；限流 Retry-After 也受该上限约束。未轮到的来源保留到期状态，下一轮继续处理。禁用 Feed 的发现不会自动取消既有 Watch：已注册适配器仍可刷新关注项；若适配器已移除，来源会标记 `skipped` 并延期复查，不计为源站失败。

同步细分最多使用 `max_window_splits` 层级和 `max_segments_per_run` 个子段，并受 `refresh_budget_sec` / `scheduled_budget_sec` 总预算约束。高密度窗口仍无法进一步证明完整时，`status` 保持 `partial` 并在状态接口显示原因。稳定游标来源把中断页 Token 与成功 checkpoint 分开持久化；状态只报告覆盖水位和分段，不暴露 opaque cursor。只有具备服务端或客户端时间过滤的 window Job 填充时间覆盖；cursor、poll 和不支持发现的适配器不会伪造时间水位，skipped 状态会保留在 Job 状态中。

## systemd 用户定时任务

先将项目安装到用户级入口。按本机 Conda Python 创建独立命令环境：

```bash
uv tool install --python 3.12 --editable .
info-desk init
command -v info-desk
mkdir -p ~/.config/systemd/user
cp deploy/information-desk.service ~/.config/systemd/user/
cp deploy/information-desk.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now information-desk.timer
```

服务将 15 分钟同步预算与 16 分钟 systemd 启动超时对齐，并在启动后五分钟补跑；每六小时附带最多十分钟随机延迟。服务使用 `UMask=0077`，停止信号为 SIGINT，锁和数据库均落在同一 `INFO_DESK_HOME`。部署前可验证当前安装入口、单元语法和隔离数据目录：

```bash
INFO_DESK_BIN="$(command -v info-desk)" deploy/validate-systemd.sh
```

脚本优先通过可用的用户级 systemd manager 执行临时 `init`/`doctor`，只写入自动创建并清理的临时目录；它不启用实际用户 Timer，也不代表完成跨日运行观察。

模板以 `%h/.local/bin/info-desk` 启动同步，每 6 小时运行一次，并在系统离线后补跑。检查记录：

```bash
systemctl --user status information-desk.timer
systemctl --user status information-desk.service
journalctl --user -u information-desk.service
```

## 备份恢复

通过 `info-desk backup <目标路径>` 调用 SQLite 在线备份 API。数据库使用 WAL；不要只复制运行中的 `intel.db` 主文件。恢复前停止定时同步及 MCP 写入进程，将备份文件放回配置指定的数据目录，再运行 `info-desk doctor`。

## 常见状态

- `success` 表示对应查询范围完整扫描。
- `partial` 表示已保留结构化结果，但仍有未完整枚举的分页或窗口。
- `failed` 表示本次作业未取得可用扫描结果。
- `skipped` 表示受能力或预算约束，本次没有启动扫描；它不会推进水位线。
- GitHub Search 到达 1,000 条或报告不完整时，适配器保留不完整状态，不推进该窗口。
- 事件按全局递增 ID 读取。日报或其他消费者各自保存 `last_event_id`，成功处理后再前移游标。
