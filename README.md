# information-desk

A local-first research and open-source intelligence service for AI agents. The service collects records from arXiv, Hugging Face and GitHub, stores them in SQLite, and exposes query and curation tools over MCP.

The first implementation is in progress. The architecture document remains the acceptance baseline, and the release gates in its test matrix have not yet been completed.

## Development

The default local interpreter is the Conda `mujoco` environment.

```bash
mujoco_python="$(conda run -n mujoco python -c 'import sys; print(sys.executable)')"
uv sync --python "$mujoco_python"
uv run info-desk doctor
uv run info-desk serve --transport stdio --profile read-only
```

Set `INFO_DESK_HOME` to choose the data directory. GitHub and Hugging Face tokens are read from `GITHUB_TOKEN` and `HF_TOKEN`.

## Design documents

- [Architecture baseline](information-desk-architecture-v1.1.md)
- [MCP interface](docs/mcp-interface.md)
- [Project structure](docs/project-structure.md)
