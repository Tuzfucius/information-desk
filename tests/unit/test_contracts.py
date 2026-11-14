from __future__ import annotations

import pytest
from pydantic import ValidationError

from info_desk.contracts import (
    EventsRequest,
    FetchRequest,
    InspectRequest,
    SaveRequest,
    SearchRequest,
)


@pytest.mark.parametrize(
    "payload",
    [
        {"limit": 0},
        {"limit": 101},
        {"keyword": "  "},
        {"item_id": 1, "keyword": "mixed query"},
        {"published_from": "2026-01-01T00:00:00", "published_before": "2026-01-02T00:00:00Z"},
        {"published_from": "2026-02-01T00:00:00Z", "published_before": "2026-01-02T00:00:00Z"},
    ],
)
def test_search_contract_rejects_invalid_bounds_and_ambiguous_queries(payload) -> None:
    with pytest.raises(ValidationError):
        SearchRequest.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"item_id": 1},
        {"item_id": 1, "expected_revision": 0},
        {"item_id": 1, "expected_revision": 0, "notes": "a", "source": {"target": "x"}},
        {"item_id": 1, "expected_revision": 0, "notes": "a", "extra": "field"},
        {"source": {"target": "x", "secret": "value"}},
        {"item_id": 1, "expected_revision": 0, "category": "  "},
    ],
)
def test_save_contract_rejects_invalid_or_extra_fields(payload) -> None:
    with pytest.raises(ValidationError):
        SaveRequest.model_validate(payload)


def test_fetch_events_and_inspect_contract_boundaries() -> None:
    assert FetchRequest(days=14, limit=100, topics=["VLA"]).days == 14
    assert EventsRequest(after_id=0, limit=100).after_id == 0
    assert InspectRequest(target="x" * 2048).target.endswith("x")
    for create in (
        lambda: FetchRequest(days=15),
        lambda: FetchRequest(feeds=["arxiv", "arxiv"]),
        lambda: EventsRequest(after_id=-1),
        lambda: InspectRequest(target="x" * 2049),
    ):
        with pytest.raises(ValidationError):
            create()
