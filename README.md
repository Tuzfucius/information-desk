# information-desk

## Project Overview

A local-first research and open-source intelligence service for AI agents. It collects records from arXiv, Hugging Face and GitHub, stores them in SQLite, and exposes query and curation tools over MCP.

## Features

- Source discovery, resumable synchronization, and watch scheduling.
- Local search, events, curation, and SQLite backups.
- MCP stdio server with read-only and standard profiles.

## Requirements

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). Python 3.11 is the minimum allowed version; CI tests 3.11 and 3.12, with 3.12 the default. Linux is the primary deployment target. Windows has not been verified and the application uses `fcntl` locks; the scheduled deployment uses systemd.

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
      "args": ["serve", "--transport", "stdio", "--profile", "read-only"],
      "env": {
        "INFO_DESK_HOME": "/home/username/.information-desk"
      }
    }
  }
}
```

`read-only` exposes local search, events and status; `standard` also permits source inspection, curation and network refresh. Inject tokens through the host's environment configuration when needed. Stdout is reserved for MCP protocol messages; diagnostics go to stderr or log files. The SDK stdio subprocess is tested; a specific end-user MCP host has not yet been verified.

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

An optional bounded public API check uses `INFO_DESK_LIVE=1 uv run --locked pytest -m live`; it requests one page per source and does not open a database. See [validation results](docs/validation-results.md).

## Deployment

For a user-level systemd timer and independent CLI installation, see [operations](docs/operations.md). Cross-day systemd operation has not been verified.

## Documentation

- [Operations](docs/operations.md)
- [MCP interface](docs/mcp-interface.md)
- [Adding a source](docs/adding-a-source.md)
- [Architecture](information-desk-architecture-v1.1.md)
- [Project structure](docs/project-structure.md)
