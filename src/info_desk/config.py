"""Small TOML and environment configuration loader."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic import ValidationError as PydanticValidationError


class Settings(BaseModel):
    home: Path
    database: Path | None = None
    enabled_feeds: list[str] = Field(
        default_factory=lambda: ["arxiv", "hf_papers", "hf_models", "github"]
    )
    topics: list[str] = Field(
        default_factory=lambda: ["Agent", "VLM", "VLA", "Diffusion Policy"]
    )
    connect_timeout_sec: float = Field(default=5, gt=0, le=120)
    read_timeout_sec: float = Field(default=20, gt=0, le=600)
    max_response_bytes: int = Field(default=4_000_000, ge=1024, le=100_000_000)
    max_retries: int = Field(default=3, ge=0, le=10)
    refresh_budget_sec: float = Field(default=30, gt=0, le=3600)
    scheduled_budget_sec: float = Field(default=900, gt=0, le=86400)
    initial_lookback_days: int = Field(default=2, ge=1, le=14)
    overlap_hours: int = Field(default=48, ge=0, le=720)
    max_pages_per_window: int = Field(default=20, ge=1, le=500)
    watch_refresh_interval_hours: int | None = Field(default=None, ge=1, le=8760)
    watch_refresh_limit_per_run: int = Field(default=100, ge=1, le=10000)
    watch_item_budget_sec: float = Field(default=20, gt=0, le=300)
    watch_failure_base_hours: int = Field(default=1, ge=1, le=168)
    watch_failure_max_hours: int = Field(default=24, ge=1, le=720)
    sync_max_window_splits: int = Field(default=16, ge=0, le=32)
    sync_max_segments_per_run: int = Field(default=100, ge=1, le=10000)
    github_token: str | None = None
    hf_token: str | None = None

    @field_validator("enabled_feeds")
    @classmethod
    def validate_feeds(cls, value: list[str]) -> list[str]:
        if not value or len(value) != len(set(value)) or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", feed) for feed in value
        ):
            raise ValueError("feeds must be unique lowercase adapter identifiers")
        return value

    @field_validator("topics")
    @classmethod
    def validate_topics(cls, value: list[str]) -> list[str]:
        if not value or len(value) > 50 or any(not topic.strip() or len(topic) > 100 for topic in value):
            raise ValueError("topics must contain 1 to 50 nonblank values of at most 100 characters")
        return value

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
        "watch_refresh_interval_hours": sync.get("watch_refresh_interval_hours"),
        "watch_refresh_limit_per_run": sync.get("watch_refresh_limit_per_run", 100),
        "watch_item_budget_sec": sync.get("watch_item_budget_sec", 20),
        "watch_failure_base_hours": sync.get("watch_failure_base_hours", 1),
        "watch_failure_max_hours": sync.get("watch_failure_max_hours", 24),
        "sync_max_window_splits": sync.get("max_window_splits", 16),
        "sync_max_segments_per_run": sync.get("max_segments_per_run", 100),
        "github_token": os.environ.get("GITHUB_TOKEN"),
        "hf_token": os.environ.get("HF_TOKEN"),
    }
    try:
        return Settings.model_validate(values)
    except PydanticValidationError as error:
        raise ValueError(f"invalid configuration: {error}") from error
