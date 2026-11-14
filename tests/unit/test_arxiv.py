from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from info_desk.sources.arxiv import ArxivAdapter
from info_desk.sources.base import AdapterError

ATOM = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom' xmlns:arxiv='http://arxiv.org/schemas/atom'>
  <arxiv:totalResults>1</arxiv:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2401.00001v2</id>
    <title>  Reliable   Agents </title>
    <summary>See https://github.com/example/agent.</summary>
    <published>2026-01-01T00:00:00Z</published>
    <updated>2026-01-02T00:00:00Z</updated>
    <author><name>Researcher</name></author>
    <category term='cs.AI'/>
    <link title='pdf' href='https://arxiv.org/pdf/2401.00001'/>
  </entry>
</feed>"""


@pytest.mark.asyncio
async def test_arxiv_discovery_parses_metadata_and_builds_next_offset() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=ATOM, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ArxivAdapter(client)
        page = await adapter.discover(
            {"topics": ["robotics"], "categories": ["cs.AI"]},
            (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)),
        )

    assert page.records[0].source_uid == "2401.00001"
    assert page.records[0].title == "Reliable Agents"
    assert page.records[0].metadata["authors"] == ["Researcher"]
    assert page.records[0].metadata["pdf_url"] == "https://arxiv.org/pdf/2401.00001"
    assert page.records[0].extracted_links[0]["url"] == "https://github.com/example/agent"
    assert page.page_complete is True
    assert requests[0].url.params["start"] == "0"
    assert 'all:"robotics"' in requests[0].url.params["search_query"]


@pytest.mark.asyncio
async def test_arxiv_discovery_paginates_and_reports_missing_total_as_incomplete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["start"] == "0":
            return httpx.Response(200, text=ATOM.replace("<arxiv:totalResults>1</arxiv:totalResults>", "<arxiv:totalResults>3</arxiv:totalResults>"), request=request)
        return httpx.Response(200, text=ATOM.replace("<arxiv:totalResults>1</arxiv:totalResults>", ""), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ArxivAdapter(client, page_size=1)
        window = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC))
        first = await adapter.discover({}, window)
        second = await adapter.discover({}, window, first.next_page_token)

    assert first.next_page_token == "1"
    assert first.page_complete is False
    assert second.next_page_token == "2"
    assert second.page_complete is False
    assert second.source_response_incomplete is True
    assert second.warnings == ["ARXIV_TOTAL_MISSING"]


@pytest.mark.asyncio
async def test_arxiv_inspection_validates_target_and_missing_entry() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<feed/>", request=request))
    ) as client:
        adapter = ArxivAdapter(client)
        with pytest.raises(AdapterError, match="official arXiv"):
            await adapter.inspect("http://arxiv.org/abs/2401.00001")
        with pytest.raises(AdapterError) as raised:
            await adapter.inspect("2401.00001")

    assert raised.value.code == "NOT_FOUND"


def test_arxiv_normalize_rejects_invalid_id_and_normalizes_vintage() -> None:
    adapter = ArxivAdapter(httpx.AsyncClient())
    source = adapter.normalize({"id": "https://arxiv.org/abs/2401.00001v5", "title": "T"})
    assert source.source_uid == "2401.00001"
    with pytest.raises(AdapterError) as raised:
        adapter.normalize({"id": "not-an-arxiv-id"})
    assert raised.value.code == "INVALID_TARGET"
