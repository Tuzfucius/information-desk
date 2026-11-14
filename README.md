# information-desk

## Project Overview

A local-first research and open-source intelligence service for AI agents. It collects records from arXiv, Hugging Face and GitHub, stores them in SQLite, and exposes query and curation tools over MCP.

## Features

- Source discovery, resumable synchronization, and watch scheduling.
- Local search, events, curation, and SQLite backups.
- MCP stdio server with read-only and standard profiles.

## Requirements

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). Python 3.11 is the minimum allowed version; CI validates Ubuntu and native Windows on Python 3.11 and 3.12. macOS has a Python 3.12 portability smoke job. Python 3.12 is the default. Windows uses native Python and uv; Conda and WSL are not required. The data directory defaults to `~/.information-desk` under the current user's home. The systemd deployment below is Linux-only.

## Quick Start

```bash
git clone https://github.com/Tuzfucius/information-desk.git
cd information-desk
uv python install
uv sync --locked --no-dev
uv run --no-dev info-desk init
uv run --no-dev info-desk doctor
uv run --no-dev info-desk status
```

On Windows, run these commands in PowerShell; this uses the native Windows Python environment and a user-local data directory:

```powershell
git clone https://github.com/Tuzfucius/information-desk.git
cd information-desk

uv python install
uv sync --locked

uv run info-desk init
uv run info-desk doctor
uv run info-desk status
```

uv creates an isolated `.venv` in the project. `init` creates or migrates the database; run it before starting the MCP server. For an independent command that works outside the checkout:

```bash
uv tool install --python 3.12 .
info-desk --help
info-desk init
info-desk doctor
```

Ensure the uv tool executable directory is on your PATH; `uv tool update-shell` can configure it. See the [uv tool documentation](https://docs.astral.sh/uv/guides/tools/).

## Configuration

Data defaults to `~/.information-desk`; set `INFO_DESK_HOME` to choose another directory. Copy `config.example.toml` to the data directory as `config.toml` to customize settings. `GITHUB_TOKEN` and `HF_TOKEN` are read from the process environment; public sources work without them.

## MCP Integration

After initializing the database, configure the host with the absolute path to the CLI in the synced project `.venv`, or the independently installed uv tool executable. Replace the placeholders:

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

For a synced Windows checkout, use the Windows console entry point and a Windows data path:

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

`standard` is the default and permits source inspection, curation and network refresh as well as local search, events and status. Select `--profile read-only` to restrict the server to local queries. Inject tokens through the host's environment configuration when needed. Stdout is reserved for MCP protocol messages; diagnostics go to stderr or log files. The SDK stdio subprocess is tested; a specific end-user MCP host has not yet been verified.

Linux entry points use `.venv/bin/info-desk`; Windows uses `.venv/Scripts/info-desk.exe`. `doctor` checks POSIX private mode bits on Linux and macOS. Windows reports filesystem ACL checks as `not_checked`; it does not claim to have audited NTFS permissions.

## Third-party Integrations

### AIHOT

information-desk optionally connects to [AIHOT](https://aihot.news/) through its official [remote MCP endpoint](https://aihot.news/agent?tab=mcp). The integration is disabled by default. When enabled, its 11 read-only tools are added only to the information-desk `standard` profile; `read-only` remains local-only. AIHOT's anonymous MCP access requires no API key, but it requires network access and depends on the upstream service being available. AIHOT results are not automatically written to the local SQLite database. Retain returned attribution and source links, and verify important facts at the original source.

AIHOT is an independent third-party service. information-desk is not affiliated with or endorsed by the AIHOT project or its maintainers. The [AIHOT software repository](https://github.com/KKKKhazix/AIHOT) is published under MIT; that software license is separate from the hosted service and its [Public Usage Terms](https://aihot.news/terms). The terms permit personal non-commercial use, qualifying public-interest non-commercial use, and internal organizational use. External commercial products or services, customer delivery, resale, public mirrors, and providing AIHOT data or proxy access to third parties require prior written authorization. Third-party source content remains subject to its own rights.

Configure AIHOT explicitly in the data directory's `config.toml`:

```toml
[aihot]
enabled = true
endpoint = "https://aihot.news/api/mcp"
timeout_sec = 25
```

The endpoint is pinned to the official HTTPS URL. See [MCP interface](docs/mcp-interface.md) for tool names and [operations](docs/operations.md) for CLI examples.

## Development

```bash
uv python install
uv sync --locked
uv run ruff check .
uv run python -m compileall -q src tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [adding a source](docs/adding-a-source.md).

## Testing

```bash
uv run pytest -m "not live"
```

To reproduce CI's plugin isolation and coverage threshold:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --locked pytest -p pytest_asyncio.plugin -p pytest_cov.plugin \
  -m "not live" --cov=info_desk --cov-report=term-missing --cov-fail-under=80
```

An optional bounded public API check uses `INFO_DESK_LIVE=1 uv run --locked pytest -m live`; the AIHOT smoke test discovers tools and makes one `aihot_get_hot_topics(limit=1)` call. Other live checks request one page per source and do not open a database. To run only AIHOT's live check, use `INFO_DESK_LIVE=1 uv run --locked pytest -m live -k aihot`. See [validation results](docs/validation-results.md).

## Deployment

For a user-level systemd timer and independent CLI installation, see [operations](docs/operations.md). Cross-day systemd operation has not been verified.

## Documentation

- [Operations](docs/operations.md)
- [MCP interface](docs/mcp-interface.md)
- [Adding a source](docs/adding-a-source.md)
- [Architecture](information-desk-architecture-v1.1.md)
- [Project structure](docs/project-structure.md)
