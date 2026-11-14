from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from info_desk.exceptions import InfoDeskError
from info_desk.server import create_server


class FakeService:
    async def search_items(self, request):
        return {"items": [], "next_cursor": None}

    def list_events(self, request):
        return {"events": [], "next_after_id": request.after_id, "has_more": False}

    def get_sync_status(self, request):
        return {"jobs": [], "watch_refresh": {"last_status": "never", "pending_due": False}}

    async def inspect_source(self, request):
        return {"source": {"source_type": "arxiv", "source_uid": request.target, "external_id": request.target, "url": f"https://arxiv.org/abs/{request.target}"}, "title": "Paper", "summary": "", "checked_at": "2026-01-01T00:00:00Z", "code_status": "unknown", "signals": [], "root_files": [], "checked_depth": 0, "extracted_links": [], "content_excerpt": None, "limitations": []}

    async def save_item(self, request):
        raise InfoDeskError("REVISION_CONFLICT", "stale", details={"item_id": request.item_id})

    async def fetch_updates(self, request):
        zero = {"fetched": 0, "new_items": 0, "new_sources": 0, "new_events": 0, "conflicts": 0}
        return {
            "run_id": 1,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:00:01Z",
            "status": "success",
            "jobs": [],
            "stats": zero,
            "events_preview": [],
            "events_preview_truncated": False,
            "warnings": [],
        }


@pytest.mark.asyncio
async def test_read_only_server_direct_contract_and_validation() -> None:
    server = create_server(FakeService(), profile="read-only")
    listed = await server.list_tools()
    assert {tool.name for tool in listed} == {"search_items", "list_events", "get_sync_status"}
    search_schema = next(tool.input_schema for tool in listed if tool.name == "search_items")
    assert search_schema["properties"]["limit"]["minimum"] == 1
    assert search_schema["properties"]["limit"]["maximum"] == 100
    output = await server.call_tool("search_items", {"keyword": "paper"})
    assert output.structured_content["ok"] is True
    with pytest.raises(ToolError):
        await server.call_tool("search_items", {"limit": 0})
    invalid = await server.call_tool("search_items", {"item_id": 1, "keyword": "mixed"})
    assert invalid.structured_content["ok"] is False
    assert invalid.structured_content["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_standard_server_direct_tools_and_structured_business_error() -> None:
    server = create_server(FakeService(), profile="standard")
    assert len(await server.list_tools()) == 6
    inspected = await server.call_tool("inspect_source", {"target": "2401.00001"})
    assert inspected.structured_content["data"]["title"] == "Paper"
    conflict = await server.call_tool("save_item", {"item_id": 3, "expected_revision": 0, "notes": "x"})
    assert conflict.structured_content["error"]["code"] == "REVISION_CONFLICT"
    fetch = await server.call_tool("fetch_updates", {"days": 1, "feeds": ["arxiv"]})
    assert fetch.structured_content["data"]["run_id"] == 1


@pytest.mark.asyncio
async def test_invalid_service_output_is_an_internal_error_not_user_input_error() -> None:
    service = FakeService()
    service.search_items = lambda _request: {"wrong": "shape"}
    result = await create_server(service).call_tool("search_items", {})
    assert result.structured_content["error"]["code"] == "INTERNAL_ERROR"


def test_server_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError, match="profile"):
        create_server(FakeService(), profile="unsafe")
