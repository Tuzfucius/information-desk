# information-desk

A local-first research and open-source intelligence service for AI agents. The service collects records from arXiv, Hugging Face and GitHub, stores them in SQLite, and exposes query and curation tools over MCP.

The M5 implementation adds persistent resumable scan segments, separate durable cursor checkpoints, fair Watch scheduling with bounded backoff, deterministic canonical source fields, and an explicit Source Adapter registry. The offline suite and a bounded live smoke check cover the four current APIs. MCP has been exercised with the installed Python SDK over a real stdio subprocess; integration with a named end-user MCP host and cross-day systemd operation have not been verified.

## Development

The default local interpreter is the Conda `mujoco` environment.

```bash
mujoco_python="$(conda run -n mujoco python -c 'import sys; print(sys.executable)')"
uv sync --locked --python "$mujoco_python" --extra dev
uv run info-desk doctor
uv run info-desk serve --transport stdio --profile read-only
```

The local `mujoco` interpreter is a convenience for this workspace. The package has no MuJoCo dependency and supports Python 3.11 and 3.12.

Run the offline suite and coverage locally:

```bash
uv run --locked ruff check .
uv run --locked python -m compileall -q src tests
uv run --locked pytest -p pytest_asyncio.plugin -p pytest_cov.plugin \
  -m "not live" --cov=info_desk --cov-report=term-missing --cov-fail-under=80
```

For a deliberately bounded public API smoke check, run `INFO_DESK_LIVE=1 uv run --locked pytest -m live`. It makes one page request per source and does not open a database. The A01–A12 evidence and limitations are recorded in [the validation report](docs/validation-results.md).

See [M5 validation results](docs/validation-results.md) for the current test matrix and unresolved release checks. Follow [adding a source](docs/adding-a-source.md) to add a Feed without changing the sync engine or storage core.

Set `INFO_DESK_HOME` to choose the data directory. GitHub and Hugging Face tokens are read from `GITHUB_TOKEN` and `HF_TOKEN`.

## Design documents

- [Architecture baseline](information-desk-architecture-v1.1.md)
- [MCP interface](docs/mcp-interface.md)
- [Project structure](docs/project-structure.md)
- [Adding a source](docs/adding-a-source.md)
- [Operations](docs/operations.md)
