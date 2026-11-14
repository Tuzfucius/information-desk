from __future__ import annotations

from typing import Annotated, Any

import pytest
from mcp import Client
from mcp.server import MCPServer
from pydantic import Field

from info_desk.app import create_application
from info_desk.integrations.aihot import AIHOT_TOOL_ANNOTATIONS, AIHOTClient
from info_desk.server import create_server


@pytest.mark.asyncio
async def test_remote_results_do_not_change_local_sqlite_or_block_local_reads(settings) -> None:
    mock_upstream = MCPServer("AIHOT boundary mock")

    @mock_upstream.tool(annotations=AIHOT_TOOL_ANNOTATIONS)
    async def aihot_get_hot_topics(
        limit: Annotated[int, Field(ge=1, le=10)] = 10,
    ) -> dict[str, Any]:
        return {
            "items": [
                {
                    "rank": 1,
                    "title": "Remote only",
                    "sourceCount": 3,
                    "links": {
                        "aihot": "https://aihot.news/items/example",
                        "original": "https://example.com/original",
                    },
                }
            ]
        }

    service, repository, http_client = create_application(settings=settings, initialize=True)
    server = create_server(
        service,
        profile="standard",
        aihot_enabled=True,
        aihot_client_factory=lambda: AIHOTClient(
            timeout_sec=5,
            session_factory=lambda: Client(mock_upstream),
        ),
    )

    class BrokenUpstream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def list_tools(self, *, cursor=None):
            raise OSError("simulated upstream connection failure")

    broken_server = create_server(
        service,
        profile="standard",
        aihot_enabled=True,
        aihot_client_factory=lambda: AIHOTClient(
            timeout_sec=5,
            session_factory=lambda: BrokenUpstream(),
        ),
    )
    tables = ("items", "sources", "change_events", "sync_state", "sync_runs", "sync_segments")
    with repository.database.read() as connection:
        before = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in tables
        }

    try:
        remote = await server.call_tool("aihot_get_hot_topics", {"limit": 1})
        failed_remote = await broken_server.call_tool("aihot_get_hot_topics", {"limit": 1})
        local_search = await server.call_tool("search_items", {})
        local_events = await server.call_tool("list_events", {})
        local_status = await server.call_tool("get_sync_status", {})
    finally:
        await http_client.aclose()

    assert remote.structured_content["ok"] is True
    assert remote.structured_content["data"]["structuredContent"]["items"][0]["links"]["original"]
    assert failed_remote.structured_content["ok"] is False
    assert failed_remote.structured_content["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert local_search.structured_content["ok"] is True
    assert local_search.structured_content["data"]["items"] == []
    assert local_events.structured_content["ok"] is True
    assert local_events.structured_content["data"]["events"] == []
    assert local_status.structured_content["ok"] is True
    with repository.database.read() as connection:
        after = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in tables
        }
    assert after == before
