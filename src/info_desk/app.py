"""Application wiring used by CLI and MCP entry points."""

from __future__ import annotations

import os

import httpx

from info_desk.config import Settings, load_settings
from info_desk.http_client import SafeHttpClient
from info_desk.service import InformationService
from info_desk.storage.repository import Repository
from info_desk.storage.sqlite import Database


def create_application(
    *,
    settings: Settings | None = None,
    readonly: bool = False,
    initialize: bool = False,
) -> tuple[InformationService, Repository, SafeHttpClient]:
    resolved = settings or load_settings()
    if not readonly:
        resolved.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(resolved.home, 0o700)
    database = Database(resolved.database_path(), readonly=readonly)
    repository = Repository(database)
    if initialize or readonly:
        repository.initialize()

    client = SafeHttpClient(
        max_response_bytes=resolved.max_response_bytes,
        max_retries=resolved.max_retries,
        github_token=resolved.github_token,
        hf_token=resolved.hf_token,
        timeout=httpx.Timeout(
            connect=resolved.connect_timeout_sec,
            read=resolved.read_timeout_sec,
            write=10,
            pool=5,
        ),
    )
    service = InformationService(resolved, repository, client)
    return service, repository, client
