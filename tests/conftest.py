from __future__ import annotations

from pathlib import Path

import pytest

from info_desk.config import Settings
from info_desk.storage.repository import Repository
from info_desk.storage.sqlite import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        home=tmp_path,
        enabled_feeds=["arxiv"],
        topics=["VLA"],
        refresh_budget_sec=5,
        scheduled_budget_sec=10,
        max_retries=0,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    repository = Repository(Database(tmp_path / "data" / "test.db"))
    repository.initialize()
    return repository
