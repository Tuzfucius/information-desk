from __future__ import annotations

import asyncio
import socket
from typing import Annotated, Any

import pytest
import uvicorn
from mcp.server import MCPServer
from pydantic import Field

from info_desk.exceptions import InfoDeskError
from info_desk.integrations.aihot import AIHOT_TOOL_ANNOTATIONS, AIHOTClient


@pytest.mark.asyncio
async def test_streamable_http_client_calls_local_mock_mcp_server() -> None:
    mock = MCPServer("AIHOT Streamable HTTP mock")
    received = []

    @mock.tool(annotations=AIHOT_TOOL_ANNOTATIONS)
    async def aihot_get_hot_topics(
        limit: Annotated[int, Field(ge=1, le=10)] = 10,
    ) -> dict[str, Any]:
        received.append(limit)
        return {
            "items": [
                {
                    "rank": 1,
                    "title": "Mock over HTTP",
                    "sourceCount": 2,
                    "links": {"original": "https://example.com/source"},
                }
            ]
        }

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            mock.streamable_http_app(json_response=True),
            log_level="critical",
            lifespan="on",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        response = await AIHOTClient(
            endpoint=f"http://127.0.0.1:{port}/mcp",
            timeout_sec=5,
        ).call_tool("aihot_get_hot_topics", {"limit": 1})
    finally:
        server.should_exit = True
        await task
        listener.close()

    assert response.ok is True
    assert response.data["structuredContent"]["items"][0]["title"] == "Mock over HTTP"
    assert response.data["structuredContent"]["items"][0]["links"]["original"]
    assert received == [1]


@pytest.mark.asyncio
async def test_streamable_http_rate_limit_preserves_retry_after() -> None:
    async def limited_app(scope, receive, send):
        if scope["type"] != "http":
            return
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [(b"content-type", b"application/json"), (b"retry-after", b"19")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"error":"limited"}'})

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(limited_app, log_level="critical", lifespan="off")
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        with pytest.raises(InfoDeskError) as error:
            await AIHOTClient(
                endpoint=f"http://127.0.0.1:{port}/mcp",
                timeout_sec=5,
            ).call_tool("aihot_get_hot_topics", {"limit": 1})
    finally:
        server.should_exit = True
        await task
        listener.close()

    assert error.value.code == "RATE_LIMITED"
    assert error.value.retry_after_seconds == 19
