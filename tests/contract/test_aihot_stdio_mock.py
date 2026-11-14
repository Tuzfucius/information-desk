from __future__ import annotations

import os
import sys

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from info_desk.integrations.aihot import AIHOT_TOOL_NAMES


@pytest.mark.asyncio
async def test_standard_stdio_exposes_and_calls_aihot_mock_tool(tmp_path) -> None:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    source = os.path.join(root, "src")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [source, root, environment.get("PYTHONPATH", "")]
    )
    script = '''
from types import SimpleNamespace
from typing import Annotated, Any
from mcp import Client
from mcp.server import MCPServer
from pydantic import Field
from info_desk.integrations.aihot import AIHOTClient
from info_desk.integrations.aihot import AIHOT_TOOL_ANNOTATIONS
from info_desk.server import create_server

upstream = MCPServer("AIHOT stdio mock")
@upstream.tool(annotations=AIHOT_TOOL_ANNOTATIONS)
async def aihot_get_hot_topics(limit: Annotated[int, Field(ge=1, le=10)] = 10) -> dict[str, Any]:
    return {"items": [{"rank": 1, "title": "stdio mock", "links": {"original": "https://example.com"}}]}

service = SimpleNamespace(settings=SimpleNamespace(aihot_timeout_sec=5))
server = create_server(
    service,
    profile="standard",
    aihot_enabled=True,
    aihot_client_factory=lambda: AIHOTClient(session_factory=lambda: Client(upstream)),
)
server.run(transport="stdio")
'''
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", script],
        env=environment,
        cwd=str(tmp_path),
    )
    async with Client(parameters, read_timeout_seconds=10) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        result = await client.call_tool("aihot_get_hot_topics", {"limit": 1})

    assert set(AIHOT_TOOL_NAMES) <= names
    assert len(names) == 17
    assert result.is_error is False
    assert result.structured_content["ok"] is True
    assert result.structured_content["data"]["structuredContent"]["items"][0]["title"] == "stdio mock"
