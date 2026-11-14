from __future__ import annotations

import base64
from datetime import UTC, datetime

import httpx
import pytest

from info_desk.sources.base import AdapterError
from info_desk.sources.github import GitHubAdapter


def repository(repo_id: int, created_at: str) -> dict[str, object]:
    return {
        "id": repo_id,
        "full_name": f"owner/repo-{repo_id}",
        "html_url": f"https://github.com/owner/repo-{repo_id}",
        "created_at": created_at,
        "updated_at": created_at,
        "pushed_at": created_at,
    }


@pytest.mark.asyncio
async def test_github_inspection_detects_files_and_extracts_untrusted_readme_links() -> None:
    readme = b"Official paper: https://arxiv.org/abs/2401.00001\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/org/project":
            return httpx.Response(200, json={**repository(123, "2026-01-01T00:00:00Z"), "default_branch": "main"}, request=request)
        if request.url.path == "/repos/org/project/contents":
            return httpx.Response(200, json=[{"name": "train.py"}, {"name": "README.md", "path": "README.md"}], request=request)
        if request.url.path == "/repos/org/project/contents/README.md":
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(readme).decode()}, request=request)
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        inspection = await GitHubAdapter(client).inspect("https://github.com/org/project")

    assert inspection.source.source_uid == "123"
    assert inspection.source.external_id == "owner/repo-123"
    assert inspection.code_status == "detected"
    assert inspection.signals == ["file:train.py"]
    assert inspection.content_excerpt == readme.decode()
    assert inspection.extracted_links[0]["evidence_level"] == "weak"


@pytest.mark.asyncio
async def test_github_inspection_validates_target_and_handles_missing_repo() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GitHubAdapter(client)
        with pytest.raises(AdapterError) as invalid:
            await adapter.inspect("https://github.com/org/repo/issues/1")
        with pytest.raises(AdapterError) as missing:
            await adapter.inspect("org/missing")

    assert invalid.value.code == "INVALID_TARGET"
    assert missing.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_github_inspection_reports_contents_access_limitation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/org/private":
            return httpx.Response(200, json={**repository(124, "2026-01-01T00:00:00Z"), "full_name": "org/private"}, request=request)
        return httpx.Response(403, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        inspection = await GitHubAdapter(client).inspect("org/private")

    assert inspection.code_status == "unknown"
    assert "contents" in inspection.limitations[0]


@pytest.mark.asyncio
async def test_search_query_preserves_subday_time_bounds() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["q"])
        return httpx.Response(
            200,
            json={"total_count": 0, "incomplete_results": False, "items": []},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await GitHubAdapter(client).discover(
            {"topic": "VLA", "mode": "created"},
            (
                datetime(2026, 1, 1, 10, 15, 30, tzinfo=UTC),
                datetime(2026, 1, 1, 11, 45, 20, tzinfo=UTC),
            ),
        )

    assert "created:2026-01-01T10:15:30+00:00..2026-01-01T11:45:20+00:00" in queries[0]
    assert page.page_complete is True


@pytest.mark.asyncio
async def test_incomplete_search_response_never_claims_completion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "incomplete_results": True,
                "items": [repository(1, "2026-01-01T10:00:00Z")],
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await GitHubAdapter(client).discover(
            {"topic": "VLA"},
            (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
        )

    assert page.records[0].source_uid == "1"
    assert page.source_response_incomplete is True
    assert page.page_complete is False
    assert page.warnings == ["GITHUB_INCOMPLETE_RESULTS"]


@pytest.mark.asyncio
async def test_one_thousand_result_search_cap_is_partial() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["page"] == "10"
        return httpx.Response(
            200,
            json={
                "total_count": 1000,
                "incomplete_results": False,
                "items": [repository(i, "2026-01-01T10:00:00Z") for i in range(901, 1001)],
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await GitHubAdapter(client).discover(
            {"topic": "VLA"},
            (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
            "10",
        )

    assert page.source_response_incomplete is True
    assert page.page_complete is False
    assert "GITHUB_SEARCH_LIMIT" in page.warnings


@pytest.mark.asyncio
async def test_search_filters_records_to_half_open_time_window() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total_count": 3,
                "incomplete_results": False,
                "items": [
                    repository(1, "2026-01-01T10:00:00Z"),
                    repository(2, "2026-01-01T10:30:00Z"),
                    repository(3, "2026-01-01T11:00:00Z"),
                ],
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await GitHubAdapter(client).discover(
            {"topic": "VLA", "mode": "created"},
            (
                datetime(2026, 1, 1, 10, tzinfo=UTC),
                datetime(2026, 1, 1, 11, tzinfo=UTC),
            ),
        )

    assert [record.source_uid for record in page.records] == ["1", "2"]


@pytest.mark.asyncio
async def test_secondary_rate_limit_403_returns_rate_limited_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"Retry-After": "5", "X-RateLimit-Remaining": "10"},
            json={"message": "You have exceeded a secondary rate limit"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        client.max_retries = 0
        with pytest.raises(AdapterError) as raised:
            await GitHubAdapter(client, token="not-for-logs").discover(
                {"topic": "VLA"},
                (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
            )

    assert raised.value.code == "RATE_LIMITED"
    assert raised.value.retry_after_seconds == 5
    assert "not-for-logs" not in str(raised.value)


@pytest.mark.asyncio
async def test_search_reports_next_page_and_uses_pushed_time_for_updates() -> None:
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["q"])
        return httpx.Response(
            200,
            json={"total_count": 3, "incomplete_results": False, "items": [repository(2, "2026-01-01T11:00:00Z")]},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await GitHubAdapter(client, page_size=1).discover(
            {"topics": ["robotics", "VLA"], "mode": "updated"},
            (datetime(2026, 1, 1, 10, tzinfo=UTC), datetime(2026, 1, 1, 12, tzinfo=UTC)),
        )

    assert page.next_page_token == "2"
    assert page.page_complete is False
    assert "pushed:" in queries[0]
    assert "(robotics OR VLA)" in queries[0]


@pytest.mark.asyncio
async def test_search_rejects_invalid_page_response_count_and_query() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"items": [], "total_count": "bad"}, request=request))
    ) as client:
        adapter = GitHubAdapter(client)
        window = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
        with pytest.raises(AdapterError) as count_error:
            await adapter.discover({}, window)
        with pytest.raises(AdapterError) as token_error:
            await adapter.discover({}, window, "not-a-page")
        with pytest.raises(AdapterError) as query_error:
            await adapter.discover({"topic": "x" * 300}, window)

    assert count_error.value.code == "UPSTREAM_UNAVAILABLE"
    assert token_error.value.code == "INVALID_CURSOR"
    assert query_error.value.code == "INVALID_ARGUMENT"


def test_normalize_requires_stable_id_and_uses_owner_login_when_needed() -> None:
    adapter = GitHubAdapter(httpx.AsyncClient())
    source = adapter.normalize({"id": 44, "name": "repo", "owner": {"login": "owner"}, "license": {"spdx_id": "MIT"}})
    assert source.external_id == "owner/repo"
    assert source.metadata["license"] == "MIT"
    with pytest.raises(AdapterError) as raised:
        adapter.normalize({"full_name": "owner/repo"})
    assert raised.value.code == "UPSTREAM_UNAVAILABLE"
