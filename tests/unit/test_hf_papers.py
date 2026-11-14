from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from info_desk.sources.base import AdapterError
from info_desk.sources.hf_papers import HuggingFacePapersAdapter


def _paper(paper_id: str, published_at: str | None = None) -> dict[str, object]:
    paper: dict[str, object] = {"id": paper_id, "title": f"Paper {paper_id}"}
    if published_at is not None:
        paper["publishedAt"] = published_at
    return {"paper": paper}


@pytest.mark.asyncio
async def test_single_day_first_page(repository=None) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[_paper("2601.00001", "2026-01-01T12:00:00Z")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await HuggingFacePapersAdapter(client).discover(
            {},
            (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
        )

    assert [record.source_uid for record in page.records] == ["2601.00001"]
    assert page.next_page_token is None
    assert page.page_complete is True
    assert requests[0].url.params["date"] == "2026-01-01"
    assert requests[0].url.params["p"] == "0"


@pytest.mark.asyncio
async def test_scans_each_date_in_multi_day_window() -> None:
    dates: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        day = request.url.params["date"]
        dates.append(day)
        return httpx.Response(200, json=[_paper(f"2601.{len(dates):05d}", f"{day}T12:00:00Z")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await HuggingFacePapersAdapter(client).discover(
            {},
            (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 4, tzinfo=UTC)),
        )

    assert dates == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert len(page.records) == 3
    assert page.page_complete is True


@pytest.mark.asyncio
async def test_follows_page_token_then_stops_on_short_page() -> None:
    page_numbers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["p"]
        page_numbers.append(page)
        if page == "0":
            return httpx.Response(
                200,
                json=[_paper(f"2601.{index:05d}", "2026-01-01T12:00:00Z") for index in range(1, 101)],
            )
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HuggingFacePapersAdapter(client)
        window = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
        first = await adapter.discover({}, window)
        second = await adapter.discover({}, window, first.next_page_token)

    assert first.next_page_token == "0:1"
    assert first.page_complete is False
    assert len(first.records) == 100
    assert second.next_page_token is None
    assert second.page_complete is True
    assert second.records == []
    assert page_numbers == ["0", "1"]


@pytest.mark.asyncio
async def test_empty_response_is_a_complete_page() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    ) as client:
        page = await HuggingFacePapersAdapter(client).discover(
            {},
            (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
        )

    assert page.records == []
    assert page.page_complete is True
    assert page.next_page_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500])
async def test_http_failure_is_not_reported_as_empty(status_code: int) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    request = httpx.Request("GET", "https://huggingface.co/api/daily_papers")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request, json={"error": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        client.max_retries = 0
        monkey = pytest.MonkeyPatch()
        monkey.setattr("info_desk.sources.base.asyncio.sleep", no_sleep)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await HuggingFacePapersAdapter(client).discover(
                    {},
                    (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
                )
        finally:
            monkey.undo()


@pytest.mark.asyncio
async def test_timeout_is_propagated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        client.max_retries = 0
        with pytest.raises(httpx.ReadTimeout):
            await HuggingFacePapersAdapter(client).discover(
                {},
                (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)),
            )


@pytest.mark.asyncio
async def test_date_window_is_start_inclusive_and_end_exclusive() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _paper("2601.00001", "2026-01-01T10:00:00Z"),
                _paper("2601.00002", "2026-01-01T11:00:00Z"),
                _paper("2601.00003", "2026-01-01T12:00:00Z"),
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await HuggingFacePapersAdapter(client).discover(
            {},
            (datetime(2026, 1, 1, 10, tzinfo=UTC), datetime(2026, 1, 1, 12, tzinfo=UTC)),
        )

    assert [record.source_uid for record in page.records] == ["2601.00001", "2601.00002"]


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["bad", "-1:0", "0:-1", "3:0"])
async def test_rejects_invalid_or_out_of_range_page_tokens(token: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    ) as client:
        with pytest.raises(AdapterError, match="page token"):
            await HuggingFacePapersAdapter(client).discover(
                {},
                (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)),
                token,
            )


def test_normalize_collects_authors_metadata_and_deduplicated_official_links() -> None:
    adapter = HuggingFacePapersAdapter(httpx.AsyncClient())
    source = adapter.normalize(
        {
            "paper": {
                "id": "https://arxiv.org/abs/2401.00123v2",
                "title": "  Paper   title ",
                "abstract": "Abstract text",
                "publishedAt": "2026-01-01T12:00:00Z",
                "authors": [{"name": "A"}, "B"],
                "upvotes": "8",
                "githubRepo": ["org/code", {"url": "https://github.com/org/code"}, "https://example.com/nope"],
            }
        }
    )
    assert source.source_uid == "2401.00123"
    assert source.title == "Paper title"
    assert source.metadata["authors"] == ["A", "B"]
    assert source.metadata["upvotes"] == 8
    assert source.metadata["github_repos"] == ["https://github.com/org/code"]
    assert source.extracted_links[0]["evidence_level"] == "supporting"


@pytest.mark.asyncio
async def test_inspect_maps_official_paper_and_source_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/2401.00123"):
            return httpx.Response(
                200,
                json={"paper": {"id": "2401.00123", "title": "Paper", "githubRepo": "org/code"}},
                request=request,
            )
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HuggingFacePapersAdapter(client)
        inspection = await adapter.inspect("https://huggingface.co/papers/2401.00123")
        with pytest.raises(AdapterError) as missing:
            await adapter.inspect("https://huggingface.co/papers/2401.99999")
        with pytest.raises(AdapterError) as invalid:
            await adapter.inspect("https://huggingface.co/org/model")

    assert inspection.source.source_uid == "2401.00123"
    assert inspection.extracted_links[0]["url"] == "https://github.com/org/code"
    assert missing.value.code == "NOT_FOUND"
    assert invalid.value.code == "INVALID_TARGET"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", ["2026-01-01"]),
        ("2026-01-01T10:00:00-05:00", "2026-01-02T10:00:00-05:00", ["2026-01-01", "2026-01-02"]),
    ],
)
async def test_window_day_expansion_respects_utc_boundaries(start, end, expected) -> None:
    dates = []

    def handler(request: httpx.Request) -> httpx.Response:
        dates.append(request.url.params["date"])
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await HuggingFacePapersAdapter(client).discover(
            {}, (datetime.fromisoformat(start), datetime.fromisoformat(end))
        )

    assert dates == expected
    assert page.page_complete is True


def test_normalize_rejects_missing_arxiv_identity() -> None:
    with pytest.raises(AdapterError) as raised:
        HuggingFacePapersAdapter(httpx.AsyncClient()).normalize({"paper": {"title": "no id"}})
    assert raised.value.code == "UPSTREAM_UNAVAILABLE"
