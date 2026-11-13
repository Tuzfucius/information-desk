# 本地部署与运行

## 环境准备

项目默认使用本机 Conda 的 `mujoco` Python。项目依赖由 uv 创建并锁定在仓库本地环境中。

```bash
mujoco_python="$(conda run -n mujoco python -c 'import sys; print(sys.executable)')"
uv sync --python "$mujoco_python"
uv run info-desk init
uv run info-desk doctor
```

如果已有 `~/.information-desk/config.toml`，可以从仓库根目录的 `config.example.toml` 复制并调整。设置 `INFO_DESK_HOME` 后该路径优先于 TOML 的 `data.home`。相对的 `data.database` 路径相对于数据目录解析。

凭证通过当前进程环境变量传入。公开资源无需凭证，配置 GitHub Token 可提高查询额度。日志不得写入 Token。

```bash
export GITHUB_TOKEN="..."
export HF_TOKEN="..."
```

## MCP 宿主

先执行 `uv sync` 和 `info-desk init`。宿主启动命令使用项目环境中的入口路径，并设置数据目录：

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

搜索默认排除 `ignored`。按 Item ID 查询以及冲突处理由 MCP 工具或后续管理命令提供。同步按页写入来源和事件。完整扫描后才推进对应作业水位线，失败区间在下次运行重扫。

## systemd 用户定时任务

先将项目安装到用户级入口。按本机 Conda Python 创建独立命令环境：

```bash
mujoco_python="$(conda run -n mujoco python -c 'import sys; print(sys.executable)')"
uv tool install --python "$mujoco_python" --editable .
info-desk init
mkdir -p ~/.config/systemd/user
cp deploy/information-desk.service ~/.config/systemd/user/
cp deploy/information-desk.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now information-desk.timer
```

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
- GitHub Search 到达 1,000 条或报告不完整时，适配器保留不完整状态，不推进该窗口。
- 事件按全局递增 ID 读取。日报或其他消费者各自保存 `last_event_id`，成功处理后再前移游标。
