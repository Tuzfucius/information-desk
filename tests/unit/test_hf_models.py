from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from info_desk.sources.base import AdapterError
from info_desk.sources.hf_models import HuggingFaceModelsAdapter


def _model(model_id: str, updated: str = "2026-01-02T00:00:00Z") -> dict[str, object]:
    return {
        "id": model_id,
        "createdAt": "2025-12-01T00:00:00Z",
        "lastModified": updated,
        "likes": "9",
        "downloads": "100",
        "tags": ["arxiv:2401.00001", "text-generation"],
        "cardData": {"title": "Agent", "description": "Model description"},
    }


@pytest.mark.asyncio
async def test_hf_model_discovery_uses_cursor_and_filters_window() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Link": '<https://huggingface.co/api/models?cursor=next>; rel="next"'},
            json=[_model("Org/Recent")] if not request.url.params.get("cursor") else [_model("org/old", "2025-12-31T23:59:00Z")],
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HuggingFaceModelsAdapter(client, page_size=1)
        start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)
        page = await adapter.discover({"topic": "robotics"}, (start, end))
        next_page = await adapter.discover({"topic": "robotics"}, (start, end), page.next_page_token)

    assert page.records[0].source_uid == "org/recent"
    assert page.next_page_token == "next"
    assert page.page_complete is False
    assert next_page.records == []
    assert requests[0].url.params["search"] == "robotics"
    assert requests[1].url.params["cursor"] == "next"


@pytest.mark.asyncio
async def test_hf_model_full_page_without_cursor_stays_incomplete() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[_model("org/a")], request=request))
    ) as client:
        page = await HuggingFaceModelsAdapter(client, page_size=1).discover(
            {}, (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC))
        )
    assert page.page_complete is False
    assert page.source_response_incomplete is True
    assert page.warnings == ["HF_MODEL_CURSOR_MISSING"]


@pytest.mark.asyncio
async def test_hf_model_inspection_detects_code_and_bounds_readme_excerpt() -> None:
    readme = ("Research https://arxiv.org/abs/2401.00001\n" + "x" * 5000).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/models/org/model":
            return httpx.Response(200, json=_model("org/model"), request=request)
        if request.url.path.endswith("/tree/main"):
            return httpx.Response(200, json=[{"path": "train.py"}, {"path": "README.md"}], request=request)
        if request.url.path.endswith("/raw/main/README.md"):
            return httpx.Response(200, content=readme, request=request)
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        inspection = await HuggingFaceModelsAdapter(client).inspect("https://huggingface.co/org/model")

    assert inspection.code_status == "detected"
    assert inspection.signals == ["file:train.py"]
    assert inspection.checked_depth == 1
    assert len(inspection.content_excerpt or "") == 4000
    assert any("truncated" in limit for limit in inspection.limitations)
    assert inspection.extracted_links[0]["url"] == "https://arxiv.org/abs/2401.00001"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["http://huggingface.co/org/model", "datasets/org/model", "org/model/not-a-supported-path"])
async def test_hf_model_rejects_non_model_or_unsafe_targets(target: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))) as client:
        with pytest.raises(AdapterError) as raised:
            await HuggingFaceModelsAdapter(client).inspect(target)
    assert raised.value.code == "INVALID_TARGET"


def test_hf_model_normalizes_card_data_and_handles_non_integer_counts() -> None:
    adapter = HuggingFaceModelsAdapter(httpx.AsyncClient())
    source = adapter.normalize({**_model("org/model"), "likes": "many", "downloads": None})
    assert source.title == "Agent"
    assert source.metadata["likes"] is None
    assert source.metadata["downloads"] is None
    assert source.extracted_links[0]["evidence_origin"] == "huggingface_model_tag"
