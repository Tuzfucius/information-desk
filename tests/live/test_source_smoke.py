from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta

import pytest

from info_desk.http_client import SafeHttpClient
from info_desk.sources.arxiv import ArxivAdapter
from info_desk.sources.github import GitHubAdapter
from info_desk.sources.hf_models import HuggingFaceModelsAdapter
from info_desk.sources.hf_papers import HuggingFacePapersAdapter

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_bounded_live_source_smoke() -> None:
    if os.environ.get("INFO_DESK_LIVE") != "1":
        pytest.skip("Set INFO_DESK_LIVE=1 to contact the public source APIs.")

    now = datetime.now(UTC).replace(microsecond=0)
    window = (now - timedelta(days=45), now + timedelta(minutes=1))
    async with SafeHttpClient(max_retries=0, max_response_bytes=1_000_000) as client:
        arxiv = await ArxivAdapter(client, page_size=1).discover({"topics": ["robotics"]}, window)
        paper_day = datetime.combine(now.date(), time.min, tzinfo=UTC)
        papers = await HuggingFacePapersAdapter(client).discover(
            {}, (paper_day, paper_day + timedelta(days=1))
        )
        models = await HuggingFaceModelsAdapter(client, page_size=1).discover({}, window)
        github = await GitHubAdapter(client, page_size=1).discover(
            {"topic": "robotics"}, (now - timedelta(days=365), now + timedelta(minutes=1))
        )
    # Empty results can be valid; the contract is that each upstream response parsed and
    # returned a bounded page without persisting to a user database.
    assert all(page.next_page_token is None or isinstance(page.next_page_token, str) for page in (arxiv, papers, models, github))
