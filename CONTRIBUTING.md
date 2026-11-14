# Contributing

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). Python 3.11 and 3.12 are tested; the project defaults to 3.12.

```bash
uv python install
uv sync --locked
uv run ruff check .
uv run pytest -m "not live"
```

For the CI coverage and plugin isolation command, see the README. Use temporary data directories for tests. See [adding a source](docs/adding-a-source.md) for adapter design and tests. Please describe the change and its validation in your pull request, add tests for changed behavior, and ensure the offline suite and Ruff pass.
