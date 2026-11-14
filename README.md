# information-desk

A local-first research and open-source intelligence service for AI agents. The service collects records from arXiv, Hugging Face and GitHub, stores them in SQLite, and exposes query and curation tools over MCP.

The V1.1 implementation has an offline regression suite and a bounded live smoke check for all four source APIs. MCP has been exercised with the installed Python SDK over a real stdio subprocess. Integration with a named end-user MCP host has not been verified.

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

Release checks still open: no named MCP host has been configured for end-to-end interoperability, and branch coverage for `storage/repository.py` and `sync_engine.py` is below the roughly 90% target. The current line-coverage gate is 80%; see the validation report before using this as a production deployment sign-off.

Set `INFO_DESK_HOME` to choose the data directory. GitHub and Hugging Face tokens are read from `GITHUB_TOKEN` and `HF_TOKEN`.

## Design documents

- [Architecture baseline](information-desk-architecture-v1.1.md)
- [MCP interface](docs/mcp-interface.md)
- [Project structure](docs/project-structure.md)
