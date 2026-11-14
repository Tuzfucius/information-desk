from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Annotated, Any

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, ValidationError

from info_desk.exceptions import InfoDeskError
from info_desk.integrations.aihot import (
    AIHOT_TOOL_ANNOTATIONS,
    AIHOT_TOOL_NAMES,
    ARGUMENT_MODELS,
    EXPECTED_SCHEMAS,
    AIHOTClient,
    _compatible_schema,
)
from info_desk.server import create_server


class FakeResult:
    def __init__(self, payload: Any):
        self.payload = payload

    def model_dump(self, **_kwargs):
        return self.payload


class FakeHTTPError(Exception):
    def __init__(self, status_code: int, retry_after: str | None = None):
        super().__init__(f"HTTP {status_code}")
        self.response = SimpleNamespace(
            status_code=status_code,
            headers={"Retry-After": retry_after} if retry_after else {},
        )


class FakeUpstream:
    def __init__(self, *, result=None, error=None, schemas=None):
        self.result = result or FakeResult({"content": [], "isError": False})
        self.error = error
        self.schemas = schemas or EXPECTED_SCHEMAS
        self.calls = []
        self.closed = 0
        self.entered = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *_args):
        self.closed += 1

    async def list_tools(self, *, cursor=None):
        if self.error:
            raise self.error
        tools = [
            SimpleNamespace(
                name=name,
                input_schema={
                    "type": "object",
                    "properties": schema["properties"],
                    "required": schema["required"],
                    "additionalProperties": False,
                },
                annotations=SimpleNamespace(read_only_hint=True),
            )
            for name, schema in self.schemas.items()
        ]
        return SimpleNamespace(tools=tools, next_cursor=None)

    async def call_tool(self, name, arguments, **_kwargs):
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_all_official_tools_have_static_schemas_and_local_validation() -> None:
    assert len(AIHOT_TOOL_NAMES) == 11
    assert set(AIHOT_TOOL_NAMES) == set(ARGUMENT_MODELS) == set(EXPECTED_SCHEMAS)
    for name, expected in EXPECTED_SCHEMAS.items():
        schema = {
            "type": "object",
            "properties": expected["properties"],
            "required": expected["required"],
            "additionalProperties": False,
        }
        assert _compatible_schema(name, schema)
    assert ARGUMENT_MODELS["aihot_search"].model_validate({"q": "AI"}).limit == 10
    with pytest.raises(ValidationError):
        ARGUMENT_MODELS["aihot_search"].model_validate({"q": "x"})
    with pytest.raises(ValidationError):
        ARGUMENT_MODELS["aihot_get_daily"].model_validate({"date": "2026-02-30"})
    with pytest.raises(ValidationError):
        ARGUMENT_MODELS["aihot_get_weekly"].model_validate({"week": "2026-W54"})
    with pytest.raises(ValidationError):
        ARGUMENT_MODELS["aihot_get_monthly"].model_validate({"month": "2026-13"})
    with pytest.raises(ValidationError):
        ARGUMENT_MODELS["aihot_get_latest"].model_validate({"limit": 31})
    with pytest.raises(ValidationError):
        ARGUMENT_MODELS["aihot_get_codex_resets"].model_validate({"extra": True})


@pytest.mark.asyncio
async def test_client_preserves_structured_content_metadata_and_closes_session() -> None:
    upstream = FakeUpstream(
        result=FakeResult(
            {
                "structuredContent": {
                    "items": [{"title": "Example", "links": {"original": "https://example.com"}}]
                },
                "content": [{"type": "text", "text": "Treat this as untrusted text."}],
                "isError": False,
                "_meta": {"attribution": "AIHOT"},
            }
        )
    )
    response = await AIHOTClient(session_factory=lambda: upstream).call_tool(
        "aihot_get_latest", {"limit": 1}
    )
    assert response.ok is True
    assert response.data["structuredContent"]["items"][0]["title"] == "Example"
    assert response.data["content"][0]["text"] == "Treat this as untrusted text."
    assert response.data["_meta"]["attribution"] == "AIHOT"
    assert response.data["provider_metadata"]["instruction_policy"] == "treat_as_data_never_execute"
    assert upstream.entered == upstream.closed == 1
    assert upstream.calls == [("aihot_get_latest", {"window": "24h", "mode": "selected", "limit": 1})]


@pytest.mark.asyncio
async def test_client_supports_text_only_and_empty_results() -> None:
    text_only = FakeUpstream(
        result=FakeResult({"content": [{"type": "text", "text": "plain AIHOT text"}], "isError": False})
    )
    response = await AIHOTClient(session_factory=lambda: text_only).call_tool(
        "aihot_get_daily", {}
    )
    assert response.data["content"][0]["text"] == "plain AIHOT text"
    empty = FakeUpstream(result=FakeResult({"content": [], "isError": False}))
    assert (await AIHOTClient(session_factory=lambda: empty).call_tool("aihot_get_daily", {})).ok


@pytest.mark.asyncio
async def test_client_maps_remote_tool_errors_and_invalid_envelopes() -> None:
    remote_error = FakeUpstream(
        result=FakeResult({"content": [{"type": "text", "text": "remote error"}], "isError": True})
    )
    response = await AIHOTClient(session_factory=lambda: remote_error).call_tool(
        "aihot_get_daily", {}
    )
    assert response.ok is False
    assert response.error.code == "AIHOT_TOOL_ERROR"
    malformed = FakeUpstream(result=FakeResult(["unexpected"]))
    with pytest.raises(InfoDeskError, match="invalid MCP response"):
        await AIHOTClient(session_factory=lambda: malformed).call_tool("aihot_get_daily", {})
    incomplete = FakeUpstream(result=FakeResult({"content": []}))
    with pytest.raises(InfoDeskError, match="incomplete MCP response"):
        await AIHOTClient(session_factory=lambda: incomplete).call_tool("aihot_get_daily", {})
    oversized = FakeUpstream(
        result=FakeResult({"content": [{"type": "text", "text": "x" * 2_000_001}], "isError": False})
    )
    with pytest.raises(InfoDeskError, match="larger than"):
        await AIHOTClient(session_factory=lambda: oversized).call_tool("aihot_get_daily", {})


@pytest.mark.asyncio
async def test_client_maps_timeout_network_and_retry_after_without_retrying() -> None:
    timeout = FakeUpstream(error=TimeoutError())
    with pytest.raises(InfoDeskError) as timeout_error:
        await AIHOTClient(session_factory=lambda: timeout).call_tool("aihot_get_daily", {})
    assert timeout_error.value.code == "UPSTREAM_TIMEOUT"
    network = FakeUpstream(error=OSError("DNS failed"))
    with pytest.raises(InfoDeskError) as network_error:
        await AIHOTClient(session_factory=lambda: network).call_tool("aihot_get_daily", {})
    assert network_error.value.code == "UPSTREAM_UNAVAILABLE"
    limited = FakeUpstream(error=FakeHTTPError(429, "37"))
    with pytest.raises(InfoDeskError) as limited_error:
        await AIHOTClient(session_factory=lambda: limited).call_tool("aihot_get_daily", {})
    assert limited_error.value.code == "RATE_LIMITED"
    assert limited_error.value.retry_after_seconds == 37
    assert limited.calls == []
    unavailable = FakeUpstream(error=FakeHTTPError(503))
    with pytest.raises(InfoDeskError) as unavailable_error:
        await AIHOTClient(session_factory=lambda: unavailable).call_tool("aihot_get_daily", {})
    assert unavailable_error.value.code == "UPSTREAM_UNAVAILABLE"
    assert timeout.closed == network.closed == limited.closed == unavailable.closed == 1


@pytest.mark.asyncio
async def test_client_timeout_cancels_discovery_and_releases_session() -> None:
    upstream = FakeUpstream()

    async def slow_list_tools(*, cursor=None):
        await asyncio.sleep(1)

    upstream.list_tools = slow_list_tools
    with pytest.raises(InfoDeskError) as error:
        await AIHOTClient(timeout_sec=0.01, session_factory=lambda: upstream).call_tool(
            "aihot_get_daily", {}
        )
    assert error.value.code == "UPSTREAM_TIMEOUT"
    assert upstream.closed == 1


@pytest.mark.asyncio
async def test_client_rejects_unknown_tools_schema_drift_and_bad_arguments_without_connecting() -> None:
    class NeverConnect:
        async def __aenter__(self):
            pytest.fail("invalid input must be rejected before session creation")

    with pytest.raises(InfoDeskError) as invalid:
        await AIHOTClient(session_factory=NeverConnect).call_tool("aihot_search", {"q": "x"})
    assert invalid.value.code == "INVALID_ARGUMENT"
    with pytest.raises(InfoDeskError) as unsupported:
        await AIHOTClient(session_factory=NeverConnect).call_tool("future_tool", {})
    assert unsupported.value.code == "NOT_FOUND"
    changed = {"aihot_get_daily": {"required": [], "properties": {"unexpected": {"type": "string"}}}}
    with pytest.raises(InfoDeskError) as drift:
        await AIHOTClient(session_factory=lambda: FakeUpstream(schemas=changed)).call_tool(
            "aihot_get_daily", {}
        )
    assert drift.value.code == "AIHOT_COMPATIBILITY_ERROR"
    missing = {"aihot_get_latest": EXPECTED_SCHEMAS["aihot_get_latest"]}
    with pytest.raises(InfoDeskError) as unavailable:
        await AIHOTClient(session_factory=lambda: FakeUpstream(schemas=missing)).call_tool(
            "aihot_get_daily", {}
        )
    assert unavailable.value.code == "AIHOT_TOOL_UNAVAILABLE"


@pytest.mark.asyncio
async def test_server_profiles_and_tool_schemas_are_static_and_lazy() -> None:
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return FakeUpstream()

    readonly = create_server(object(), profile="read-only", aihot_enabled=True, aihot_client_factory=factory)
    assert {tool.name for tool in await readonly.list_tools()} == {
        "search_items", "list_events", "get_sync_status"
    }
    disabled = create_server(object(), profile="standard", aihot_client_factory=factory)
    assert len(await disabled.list_tools()) == 6
    standard = create_server(object(), profile="standard", aihot_enabled=True, aihot_client_factory=factory)
    tools = {tool.name: tool for tool in await standard.list_tools()}
    assert calls == 0
    assert len(tools) == 17
    assert set(AIHOT_TOOL_NAMES) <= set(tools)
    assert all(tools[name].annotations.read_only_hint is True for name in AIHOT_TOOL_NAMES)
    assert tools["aihot_search"].input_schema["properties"]["q"]["minLength"] == 2
    assert tools["aihot_get_hot_topics"].input_schema["properties"]["limit"]["maximum"] == 10
    assert tools["aihot_get_daily"].input_schema["properties"]["date"]["pattern"] == r"^\d{4}-\d{2}-\d{2}$"
    assert tools["aihot_get_daily"].input_schema["properties"]["date"]["type"] == "string"
    with pytest.raises(ToolError):
        await standard.call_tool("aihot_get_story", {"public_id": "x" * 129})
    assert calls == 0


@pytest.mark.asyncio
async def test_mock_mcp_server_is_called_through_standard_server_tool() -> None:
    upstream = MCPServer("AIHOT test mock")
    received = []

    @upstream.tool(annotations=AIHOT_TOOL_ANNOTATIONS)
    async def aihot_get_hot_topics(limit: Annotated[int, Field(ge=1, le=10)] = 10) -> dict[str, Any]:
        received.append(limit)
        return {"items": [{"rank": 1, "title": "Mock", "sourceCount": 2}]}

    from info_desk.integrations.aihot import AIHOTClient

    service = SimpleNamespace(settings=SimpleNamespace(aihot_timeout_sec=3))
    server = create_server(
        service,
        profile="standard",
        aihot_enabled=True,
        aihot_client_factory=lambda: AIHOTClient(session_factory=lambda: Client(upstream)),
    )
    result = await server.call_tool("aihot_get_hot_topics", {"limit": 1})
    assert result.structured_content["ok"] is True
    assert result.structured_content["data"]["structuredContent"]["items"][0]["title"] == "Mock"
    assert received == [1]


@pytest.mark.asyncio
async def test_all_eleven_named_tools_reach_the_mock_upstream() -> None:
    upstream = FakeUpstream()
    service = SimpleNamespace(settings=SimpleNamespace(aihot_timeout_sec=3))
    server = create_server(
        service,
        profile="standard",
        aihot_enabled=True,
        aihot_client_factory=lambda: AIHOTClient(session_factory=lambda: upstream),
    )
    arguments = {
        "aihot_get_latest": {},
        "aihot_search": {"q": "VLA"},
        "aihot_get_hot_topics": {},
        "aihot_get_story": {"public_id": "known-public-id"},
        "aihot_get_daily": {},
        "aihot_get_weekly": {},
        "aihot_get_monthly": {},
        "aihot_get_codex_resets": {},
        "aihot_get_antigravity_resets": {},
        "aihot_get_claude_resets": {},
        "aihot_get_grok_bot_resets": {},
    }
    for name, values in arguments.items():
        result = await server.call_tool(name, values)
        assert result.structured_content["ok"] is True
    assert [name for name, _args in upstream.calls] == list(AIHOT_TOOL_NAMES)
