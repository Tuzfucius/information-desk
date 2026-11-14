from __future__ import annotations

import httpx
import pytest

from info_desk.exceptions import InfoDeskError, UpstreamError
from info_desk.inspect import SourceInspector
from info_desk.sources.base import AdapterError, CandidateSource, SourceInspection


class Adapter:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.targets = []

    async def inspect(self, target):
        self.targets.append(target)
        if self.error:
            raise self.error
        return self.result


def _inspection(excerpt=None):
    source = CandidateSource(
        source_type="arxiv", source_uid="2401.00001", external_id="2401.00001",
        url="https://arxiv.org/abs/2401.00001", title="Paper", description="Abstract",
    )
    return SourceInspection(source=source, checked_at="2026-01-01T00:00:00Z", content_excerpt=excerpt)


@pytest.mark.asyncio
async def test_inspector_routes_supported_targets_and_returns_source_contract() -> None:
    arxiv = Adapter(_inspection())
    github = Adapter(_inspection())
    hf_papers = Adapter(_inspection())
    hf_models = Adapter(_inspection())
    inspector = SourceInspector(
        {"arxiv": arxiv, "github": github, "hf_papers": hf_papers, "hf_models": hf_models},
        lambda response: UpstreamError("UPSTREAM_UNAVAILABLE", str(response.status_code)),
    )
    result = await inspector.inspect("2401.00001")
    assert result["source"]["source_uid"] == "2401.00001"
    assert result["title"] == "Paper"
    assert result["summary"] == "Abstract"
    assert arxiv.targets == ["2401.00001"]
    await inspector.inspect("https://github.com/org/repo")
    await inspector.inspect("https://huggingface.co/papers/2401.00001")
    await inspector.inspect("https://huggingface.co/org/model")
    assert github.targets and hf_papers.targets and hf_models.targets


@pytest.mark.asyncio
async def test_inspector_rejects_unsupported_hosts_and_maps_adapter_and_http_errors() -> None:
    adapters = {name: Adapter(_inspection()) for name in ("arxiv", "github", "hf_papers", "hf_models")}
    inspector = SourceInspector(adapters, lambda response: UpstreamError("UPSTREAM_UNAVAILABLE", str(response.status_code)))
    with pytest.raises(InfoDeskError) as unsupported:
        await inspector.inspect("https://example.com/repo")
    assert unsupported.value.code == "UNSUPPORTED_SOURCE"

    adapters["arxiv"].error = AdapterError("NOT_FOUND", "missing")
    with pytest.raises(InfoDeskError) as missing:
        await inspector.inspect("2401.00001")
    assert missing.value.code == "NOT_FOUND"

    adapters["arxiv"].error = httpx.ReadTimeout("timeout")
    with pytest.raises(UpstreamError) as timeout:
        await inspector.inspect("2401.00001")
    assert timeout.value.code == "UPSTREAM_TIMEOUT"

    request = httpx.Request("GET", "https://export.arxiv.org/api/query")
    adapters["arxiv"].error = httpx.HTTPStatusError("bad status", request=request, response=httpx.Response(503, request=request))
    with pytest.raises(UpstreamError) as unavailable:
        await inspector.inspect("2401.00001")
    assert unavailable.value.code == "UPSTREAM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_inspector_marks_external_excerpt_untrusted_and_truncated() -> None:
    adapters = {name: Adapter(_inspection("x" * 4001)) for name in ("arxiv", "github", "hf_papers", "hf_models")}
    result = await SourceInspector(adapters, lambda _response: None).inspect("2401.00001")
    excerpt = result["content_excerpt"]
    assert len(excerpt["text"]) == 4000
    assert excerpt["truncated"] is True
    assert excerpt["trust"] == "untrusted_external"
