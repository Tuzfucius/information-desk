from __future__ import annotations

import os

import pytest

from info_desk.integrations.aihot import AIHOTClient

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_bounded_aihot_streamable_http_smoke() -> None:
    if os.environ.get("INFO_DESK_LIVE") != "1":
        pytest.skip("Set INFO_DESK_LIVE=1 to contact AIHOT.")

    result = await AIHOTClient(timeout_sec=25).call_tool("aihot_get_hot_topics", {"limit": 1})
    assert result.ok is True
    assert result.data is not None
    assert result.data["isError"] is False
    assert result.data.get("structuredContent") is not None or result.data.get("content")
