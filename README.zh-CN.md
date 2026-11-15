# information-desk

[English](README.md)

## 项目简介

一个本地优先的研究与开源情报服务，供 AI 智能体使用。它从 arXiv、Hugging Face 和 GitHub 收集记录，将数据存储在 SQLite 中，并通过 MCP 提供查询与整理工具。

## 功能

- 发现数据源、断点续传式同步和定时监控。
- 本地搜索、事件记录、内容整理和 SQLite 备份。
- 提供只读和标准配置的 MCP stdio 服务端。

## 环境要求

请先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。最低支持 Python 3.11；CI 会在 Ubuntu 和原生 Windows 上验证 Python 3.11 与 3.12，并在 macOS 上执行 Python 3.12 可移植性检查。默认使用 Python 3.12。Windows 使用原生 Python 和 uv，无需 Conda 或 WSL。

数据目录默认位于当前用户主目录下的 `~/.information-desk`。下文的 systemd 部署方式仅适用于 Linux。

## 快速开始

```bash
git clone https://github.com/Tuzfucius/information-desk.git
cd information-desk
uv python install
uv sync --locked --no-dev
uv run --no-dev info-desk init
uv run --no-dev info-desk doctor
uv run --no-dev info-desk status
```

在 Windows 上，请在 PowerShell 中运行以下命令。它会使用原生 Windows Python 环境和用户本地数据目录：

```powershell
git clone https://github.com/Tuzfucius/information-desk.git
cd information-desk

uv python install
uv sync --locked

uv run info-desk init
uv run info-desk doctor
uv run info-desk status
```

uv 会在项目目录中创建隔离的 `.venv` 环境。启动 MCP 服务端之前，请先运行 `init` 创建或迁移数据库。若希望在项目目录之外使用 CLI，可独立安装：

```bash
uv tool install --python 3.12 .
info-desk --help
info-desk init
info-desk doctor
```

请确保 uv 工具的可执行文件目录已加入 `PATH`；可以运行 `uv tool update-shell` 进行配置。详见 [uv 工具文档](https://docs.astral.sh/uv/guides/tools/)。

## 配置

数据默认存放在 `~/.information-desk`；设置 `INFO_DESK_HOME` 可更改目录。将 `config.example.toml` 复制到数据目录并命名为 `config.toml`，即可自定义设置。程序从进程环境变量读取 `GITHUB_TOKEN` 和 `HF_TOKEN`；访问公开数据源时无需配置这些令牌。

## MCP 集成

初始化数据库后，请在 MCP 客户端配置中填写同步项目 `.venv` 里的 CLI 绝对路径，或独立安装的 uv 工具可执行文件路径。请将示例中的占位路径替换为实际路径：

```json
{
  "mcpServers": {
    "information-desk": {
      "command": "/path/to/information-desk/.venv/bin/info-desk",
      "args": ["serve", "--transport", "stdio", "--profile", "standard"],
      "env": {
        "INFO_DESK_HOME": "/home/username/.information-desk"
      }
    }
  }
}
```

对于同步安装的 Windows 项目，请使用 Windows 控制台入口和 Windows 数据路径：

```json
{
  "mcpServers": {
    "information-desk": {
      "command": "C:/path/to/information-desk/.venv/Scripts/info-desk.exe",
      "args": ["serve", "--transport", "stdio", "--profile", "standard"],
      "env": {
        "INFO_DESK_HOME": "C:/Users/username/.information-desk"
      }
    }
  }
}
```

`standard` 是默认配置，除本地搜索、事件和状态查询外，还允许检查数据源、整理内容和联网刷新。使用 `--profile read-only` 可将服务端限制为本地查询。如有需要，请通过 MCP 客户端的环境配置注入令牌。标准输出专用于 MCP 协议消息；诊断信息写入标准错误或日志文件。项目已测试 SDK 的 stdio 子进程，但尚未验证特定终端用户 MCP 客户端。

Linux 的入口路径为 `.venv/bin/info-desk`；Windows 为 `.venv/Scripts/info-desk.exe`。在 Linux 和 macOS 上，`doctor` 会检查 POSIX 私有权限位。Windows 会将文件系统 ACL 检查报告为 `not_checked`，不会声称已审计 NTFS 权限。

## 第三方集成

### AIHOT

information-desk 默认通过 [AIHOT 官方远程 MCP 接口](https://aihot.news/agent?tab=mcp)连接 [AIHOT](https://aihot.news/)。它的 11 个只读工具仅添加到 information-desk 的 `standard` 配置中；`read-only` 配置仍只访问本地数据。设置 `[aihot].enabled = false` 可关闭此集成。AIHOT 匿名 MCP 访问无需 API 密钥，但需要网络连接，且依赖上游服务可用。AIHOT 返回的结果不会自动写入本地 SQLite 数据库。请保留结果中的来源标注和链接，并通过原始来源核实重要信息。

AIHOT 是独立的第三方服务。information-desk 与 AIHOT 项目及其维护者没有关联，也未获其认可。[AIHOT 软件仓库](https://github.com/KKKKhazix/AIHOT)采用 MIT 许可证；该软件许可证与托管服务的[公开使用条款](https://aihot.news/terms)相互独立。条款允许个人非商业用途、符合条件的公益非商业用途以及组织内部使用。外部商业产品或服务、向客户交付、转售、公开镜像，以及向第三方提供 AIHOT 数据或代理访问，均需事先取得书面授权。第三方来源内容仍受其各自权利约束。

如需自定义超时时间，或在数据目录的 `config.toml` 中显式启用 AIHOT：

```toml
[aihot]
enabled = true
endpoint = "https://aihot.news/api/mcp"
timeout_sec = 25
```

接口地址固定为官方 HTTPS 地址。工具名称请参见 [MCP 接口文档](docs/mcp-interface.md)，CLI 示例请参见[运维文档](docs/operations.md)。

## 开发

```bash
uv python install
uv sync --locked
uv run ruff check .
uv run python -m compileall -q src tests
```

请参阅 [贡献指南](CONTRIBUTING.md)和[添加数据源指南](docs/adding-a-source.md)。

## 测试

```bash
uv run pytest -m "not live"
```

如需复现 CI 的插件隔离与覆盖率门槛：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --locked pytest -p pytest_asyncio.plugin -p pytest_cov.plugin \
  -m "not live" --cov=info_desk --cov-report=term-missing --cov-fail-under=80
```

可选的有限范围公开 API 检查可通过 `INFO_DESK_LIVE=1 uv run --locked pytest -m live` 启用；AIHOT 冒烟测试会发现工具并调用一次 `aihot_get_hot_topics(limit=1)`。其他在线检查会从每个数据源请求一页数据，不会打开数据库。若只运行 AIHOT 在线检查，请执行 `INFO_DESK_LIVE=1 uv run --locked pytest -m live -k aihot`。详见[验证结果](docs/validation-results.md)。

## 部署

用户级 systemd 定时器和独立 CLI 安装方法请参见[运维文档](docs/operations.md)。跨日运行的 systemd 行为尚未验证。

## 文档

- [运维](docs/operations.md)
- [MCP 接口](docs/mcp-interface.md)
- [添加数据源](docs/adding-a-source.md)
- [架构](information-desk-architecture-v1.1.md)
- [项目结构](docs/project-structure.md)
