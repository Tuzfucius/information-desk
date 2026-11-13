"""Small TOML and environment configuration loader."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError


class Settings(BaseModel):
    home: Path
    database: Path | None = None
    enabled_feeds: list[str] = Field(
        default_factory=lambda: ["arxiv", "hf_papers", "hf_models", "github"]
    )
    topics: list[str] = Field(
        default_factory=lambda: ["Agent", "VLM", "VLA", "Diffusion Policy"]
    )
    connect_timeout_sec: float = 5
    read_timeout_sec: float = 20
    max_response_bytes: int = 4_000_000
    max_retries: int = 3
    refresh_budget_sec: float = 30
    scheduled_budget_sec: float = 900
    initial_lookback_days: int = 2
    overlap_hours: int = 48
    max_pages_per_window: int = 20
    watch_refresh_interval_hours: int = 24
    watch_refresh_limit_per_run: int = 100
    github_token: str | None = None
    hf_token: str | None = None

    def database_path(self) -> Path:
        if self.database is None:
            return self.home / "intel.db"
        return self.database if self.database.is_absolute() else self.home / self.database


def load_settings(config_path: Path | None = None) -> Settings:
    home = Path(os.environ.get("INFO_DESK_HOME", "~/.information-desk")).expanduser()
    if config_path is None:
        env_config = os.environ.get("INFO_DESK_CONFIG")
        config_path = Path(env_config).expanduser() if env_config else home / "config.toml"

    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as config_file:
            raw = tomllib.load(config_file)

    data = raw.get("data", {})
    http = raw.get("http", {})
    sync = raw.get("sync", {})
    configured_home = os.environ.get("INFO_DESK_HOME", data.get("home", home))
    values = {
        "home": Path(configured_home).expanduser(),
        "database": (
            Path(data["database"]).expanduser()
            if data.get("database")
            else None
        ),
        "enabled_feeds": sync.get(
            "feeds", ["arxiv", "hf_papers", "hf_models", "github"]
        ),
        "topics": sync.get("topics", ["Agent", "VLM", "VLA", "Diffusion Policy"]),
        "connect_timeout_sec": http.get("connect_timeout_sec", 5),
        "read_timeout_sec": http.get("read_timeout_sec", 20),
        "max_response_bytes": http.get("max_response_bytes", 4_000_000),
        "max_retries": http.get("max_retries", 3),
        "refresh_budget_sec": sync.get("refresh_budget_sec", 30),
        "scheduled_budget_sec": sync.get("scheduled_budget_sec", 900),
        "initial_lookback_days": sync.get("initial_lookback_days", 2),
        "overlap_hours": sync.get("overlap_hours", 48),
        "max_pages_per_window": sync.get("max_pages_per_window", 20),
        "watch_refresh_interval_hours": sync.get("watch_refresh_interval_hours", 24),
        "watch_refresh_limit_per_run": sync.get("watch_refresh_limit_per_run", 100),
        "github_token": os.environ.get("GITHUB_TOKEN"),
        "hf_token": os.environ.get("HF_TOKEN"),
    }
    try:
        return Settings.model_validate(values)
    except PydanticValidationError as error:
        raise ValueError(f"invalid configuration: {error}") from error
