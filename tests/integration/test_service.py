from __future__ import annotations

import httpx
import pytest

from info_desk.contracts import EventsRequest, SaveRequest, SearchRequest, StatusRequest
from info_desk.exceptions import InfoDeskError, RevisionConflictError
from info_desk.http_client import SafeHttpClient
from info_desk.service import InformationService


def _service(settings, repository) -> tuple[InformationService, SafeHttpClient]:
    client = SafeHttpClient(transport=httpx.MockTransport(lambda request: httpx.Response(404, request=request)))
    return InformationService(settings, repository, client), client


@pytest.mark.asyncio
async def test_service_search_cursor_is_stable_and_bound_to_filters(repository, settings) -> None:
    service, client = _service(settings, repository)
    try:
        for title in ("one", "two", "three"):
            with repository.transaction() as connection:
                repository.create_item({"title": title}, now="2026-01-01T00:00:00Z", conn=connection)

        first = await service.search_items(SearchRequest(keyword="t", limit=1))
        assert first["items"][0].title == "three"
        assert first["next_cursor"]
        second = await service.search_items(
            SearchRequest(keyword="t", limit=1, cursor=first["next_cursor"])
        )
        assert [item.title for item in second["items"]] == ["two"]
        with pytest.raises(InfoDeskError) as raised:
            await service.search_items(SearchRequest(keyword="one", limit=1, cursor=first["next_cursor"]))
        assert raised.value.code == "INVALID_CURSOR"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_service_save_is_idempotent_and_revision_conflict_preserves_notes(repository, settings) -> None:
    service, client = _service(settings, repository)
    try:
        with repository.transaction() as connection:
            item_id = repository.create_item({"title": "paper"}, now="2026-01-01T00:00:00Z", conn=connection)
        first = await service.save_item(
            SaveRequest(item_id=item_id, expected_revision=0, triage_status="saved", notes="reviewed")
        )
        unchanged = await service.save_item(
            SaveRequest(item_id=item_id, expected_revision=1, triage_status="saved", notes="reviewed")
        )
        assert first["action"] == "updated"
        assert unchanged["action"] == "unchanged"
        assert unchanged["revision"] == 1
        with pytest.raises(RevisionConflictError):
            await service.save_item(
                SaveRequest(item_id=item_id, expected_revision=0, notes="stale")
            )
        assert repository.get_item(item_id)["notes"] == "reviewed"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_service_imports_new_source_and_reports_multi_item_conflict(repository, settings) -> None:
    service, client = _service(settings, repository)
    try:
        source_data = {
            "source_type": "arxiv",
            "source_uid": "2401.00990",
            "external_id": "2401.00990",
            "url": "https://arxiv.org/abs/2401.00990",
            "title": "New source",
            "description": "Abstract",
            "metadata": {},
        }

        async def inspect(_request):
            return {"source": source_data, "title": "New source", "summary": "Abstract"}

        service.inspect_source = inspect
        created = await service.save_item(SaveRequest(source={"target": "2401.00990"}))
        assert created["action"] == "created"
        assert created["triage_status"] == "saved"
        assert repository.get_item(created["item_id"])["category"] == "general"

        with repository.transaction() as connection:
            first = repository.create_item({"title": "Paper A"}, now="2026-01-01T00:00:00Z", conn=connection)
            second = repository.create_item({"title": "Paper B"}, now="2026-01-01T00:00:00Z", conn=connection)
        github_data = {
            "source_type": "github_repo", "source_uid": "919", "external_id": "org/code",
            "url": "https://github.com/org/code", "title": "org/code", "description": "",
            "metadata": {},
            "extracted_links": [
                {"url": "https://arxiv.org/abs/2401.00091", "relation_type": "official_code", "evidence_level": "strong", "evidence_origin": "test"},
                {"url": "https://arxiv.org/abs/2401.00092", "relation_type": "official_code", "evidence_level": "strong", "evidence_origin": "test"},
            ],
        }
        for uid, item_id in (("2401.00091", first), ("2401.00092", second)):
            with repository.transaction() as connection:
                repository.upsert_source(item_id, {
                    "source_type": "arxiv", "source_uid": uid, "external_id": uid,
                    "url": f"https://arxiv.org/abs/{uid}", "title": uid,
                }, now="2026-01-01T00:00:00Z", conn=connection)

        async def inspect_repo(_request):
            candidate = dict(github_data)
            links = candidate.pop("extracted_links")
            return {"source": candidate, "title": "org/code", "summary": "", "extracted_links": links}

        service.inspect_source = inspect_repo
        with pytest.raises(InfoDeskError) as conflict:
            await service.save_item(SaveRequest(source={"target": "https://github.com/org/code"}))
        assert conflict.value.code == "MERGE_CONFLICT"
        pending_id = conflict.value.details["pending_source_id"]
        assert set(conflict.value.details["candidate_item_ids"]) == {first, second}
        assert repository.get_source("github_repo", "919") is None
        assert repository.get_pending_source(pending_id)["status"] == "open"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_service_exact_search_includes_notes_and_reports_missing_item(repository, settings) -> None:
    service, client = _service(settings, repository)
    try:
        with repository.transaction() as connection:
            item_id = repository.create_item({"title": "exact", "notes": "private note"}, now="2026-01-01T00:00:00Z", conn=connection)
        result = await service.search_items(SearchRequest(item_id=item_id))
        assert result["items"][0].notes == "private note"
        with pytest.raises(InfoDeskError) as missing:
            await service.search_items(SearchRequest(item_id=999))
        assert missing.value.code == "NOT_FOUND"
    finally:
        await client.aclose()


def test_service_events_and_status_allow_independent_consumer_cursors(repository, settings) -> None:
    service, client = _service(settings, repository)
    try:
        with repository.transaction() as connection:
            item_id = repository.create_item({"title": "event item"}, now="2026-01-01T00:00:00Z", conn=connection)
            repository.create_event(item_id, None, "discovered", "service-event", {"title": "event item"}, created_at="2026-01-01T00:00:00Z", conn=connection)
        consumer_a = service.list_events(EventsRequest(after_id=0, limit=1))
        consumer_b = service.list_events(EventsRequest(after_id=0, limit=1))
        assert consumer_a["events"][0].id == consumer_b["events"][0].id
        status = service.get_sync_status(StatusRequest(feeds=["arxiv", "github"]))
        assert [job["last_status"] for job in status["jobs"]] == ["never", "never"]
        assert status["watch_refresh"]["pending_due"] is False
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_inspection_route_converts_adapter_failures_to_structured_error(repository, settings) -> None:
    service, client = _service(settings, repository)
    try:
        response = httpx.Response(429, headers={"Retry-After": "4"}, request=httpx.Request("GET", "https://api.github.com"))
        error = service._upstream_error(response)
        assert error.code == "RATE_LIMITED"
        assert error.retry_after_seconds == 4
        assert service._candidate_from_inspection({
            "source": {
                "source_type": "github_repo", "source_uid": "9", "external_id": "a/b",
                "url": "https://github.com/a/b", "title": "a/b", "metadata": {"fork": False},
            },
            "code_status": "detected", "signals": ["file:main.py"],
        }).metadata == {"fork": False, "code_status": "detected", "code_signals": ["file:main.py"]}
    finally:
        import asyncio

        asyncio.run(client.aclose())
