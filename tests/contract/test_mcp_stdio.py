from __future__ import annotations

import os
import sys

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from info_desk.integrations.aihot import AIHOT_TOOL_NAMES
from info_desk.storage.repository import Repository
from info_desk.storage.sqlite import Database


def _parameters(home, profile: str | None) -> StdioServerParameters:
    environment = os.environ.copy()
    environment["INFO_DESK_HOME"] = str(home)
    environment.pop("GITHUB_TOKEN", None)
    environment.pop("HF_TOKEN", None)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "info_desk.cli", "serve", "--transport", "stdio"]
        + (["--profile", profile] if profile else []),
        env=environment,
        cwd=str(home.parent),
    )


def _structured(result):
    if getattr(result, "structuredContent", None) is not None:
        value = result.structuredContent
        return value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    if result.content and getattr(result.content[0], "text", None):
        import json

        return json.loads(result.content[0].text)
    raise AssertionError("MCP result did not contain structured content")


@pytest.mark.asyncio
async def test_stdio_read_only_profile_lists_calls_and_validates_three_tools(tmp_path) -> None:
    home = tmp_path / "read-only-home"
    repository = Repository(Database(home / "intel.db"))
    repository.initialize()
    if os.name != "nt":
        home.chmod(0o700)
    with repository.transaction() as connection:
        item_id = repository.create_item(
            {"title": "Needle discovery"}, now="2026-01-01T00:00:00Z", conn=connection
        )
    with repository.database.read() as connection:
        before = (
            connection.execute("SELECT count(*) FROM items").fetchone()[0],
            connection.execute("SELECT count(*) FROM change_events").fetchone()[0],
            connection.execute("SELECT count(*) FROM sources").fetchone()[0],
        )

    async with Client(_parameters(home, "read-only"), read_timeout_seconds=10) as client:
        listed = await client.list_tools()
        tool_names = {tool.name for tool in listed.tools}
        search_tool = next(tool for tool in listed.tools if tool.name == "search_items")
        output_schema = getattr(search_tool, "output_schema", None)
        search = _structured(await client.call_tool("search_items", {"keyword": "Needle"}))
        events = _structured(await client.call_tool("list_events", {"after_id": 0, "limit": 10}))
        status = _structured(await client.call_tool("get_sync_status", {}))
        invalid_schema = await client.call_tool("search_items", {"limit": 0})
        invalid = _structured(
            await client.call_tool("search_items", {"item_id": 1, "keyword": "mixed"})
        )

    assert tool_names == {"search_items", "list_events", "get_sync_status"}
    assert output_schema is not None
    assert "ok" in output_schema["properties"]
    assert "data" in output_schema["properties"]
    assert search["ok"] is True
    assert search["data"]["items"][0]["item_id"] == item_id
    assert events["data"]["next_after_id"] == 0
    assert status["ok"] is True
    assert invalid_schema.is_error is True
    assert "limit" in invalid_schema.content[0].text
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "INVALID_ARGUMENT"
    with repository.database.read() as connection:
        after = (
            connection.execute("SELECT count(*) FROM items").fetchone()[0],
            connection.execute("SELECT count(*) FROM change_events").fetchone()[0],
            connection.execute("SELECT count(*) FROM sources").fetchone()[0],
        )
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, "standard"])
async def test_stdio_standard_profile_has_typed_schemas_and_structured_business_errors(
    tmp_path, profile
) -> None:
    home = tmp_path / "standard-home"
    repository = Repository(Database(home / "intel.db"))
    repository.initialize()
    if os.name != "nt":
        home.chmod(0o700)
    with repository.transaction() as connection:
        item_id = repository.create_item(
            {"title": "Concurrency item"}, now="2026-01-01T00:00:00Z", conn=connection
        )
        repository.update_item(
            item_id,
            0,
            {"triage_status": "saved", "notes": "current"},
            now="2026-01-02T00:00:00Z",
            conn=connection,
        )

    async with Client(_parameters(home, profile), read_timeout_seconds=10) as client:
        listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        revision_error = _structured(
            await client.call_tool(
                "save_item",
                {
                    "item_id": item_id,
                    "expected_revision": 0,
                    "notes": "stale overwrite",
                },
            )
        )
        target_error = _structured(
            await client.call_tool("inspect_source", {"target": "https://example.com/repo"})
        )

    assert set(tools) == {
        "search_items",
        "list_events",
        "get_sync_status",
        "inspect_source",
        "save_item",
        "fetch_updates",
    } | set(AIHOT_TOOL_NAMES)
    for name in ("inspect_source", "save_item", "fetch_updates"):
        schema = getattr(tools[name], "output_schema", None)
        assert schema is not None
        assert "data" in schema["properties"]
    assert revision_error["ok"] is False
    assert revision_error["error"]["code"] == "REVISION_CONFLICT"
    assert revision_error["error"]["details"]["item_id"] == item_id
    assert target_error["ok"] is False
    assert target_error["error"]["code"] == "UNSUPPORTED_SOURCE"
    item = repository.get_item(item_id)
    assert item["notes"] == "current"
    assert item["revision"] == 1
