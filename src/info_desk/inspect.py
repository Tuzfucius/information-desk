"""Validated routing and normalized result shaping for source inspection."""

from __future__ import annotations

from typing import Any

import httpx

from info_desk.exceptions import InfoDeskError, UpstreamError
from info_desk.sources.base import AdapterError
from info_desk.sources.registry import AdapterRegistry


class SourceInspector:
    def __init__(self, adapters: dict[str, Any] | AdapterRegistry, upstream_error: Any) -> None:
        self.adapters = adapters if isinstance(adapters, AdapterRegistry) else AdapterRegistry.from_mapping(adapters)
        self.upstream_error = upstream_error

    async def inspect(self, target: str) -> dict[str, Any]:
        adapter = self._adapter_for_target(target)
        try:
            inspection = await adapter.inspect(target)
        except AdapterError as error:
            raise InfoDeskError(error.code, str(error)) from error
        except httpx.TimeoutException as error:
            raise UpstreamError("UPSTREAM_TIMEOUT", "The source request timed out.") from error
        except httpx.HTTPStatusError as error:
            raise self.upstream_error(error.response) from error
        except httpx.HTTPError as error:
            raise UpstreamError(
                "UPSTREAM_UNAVAILABLE", "The source request failed."
            ) from error

        feed = next(
            feed for feed in self.adapters
            if self.adapters[feed] is adapter
        )
        inspection.source.feed = feed
        if inspection.source.source_type not in self.adapters.capabilities(feed).source_types:
            raise InfoDeskError(
                "INTERNAL_ERROR", "Source adapter returned an undeclared source type."
            )
        inspection.source.watch_interval_hours = self.adapters.capabilities(
            feed
        ).watch_interval_hours

        result = inspection.to_dict()
        candidate = result.pop("source")
        result["source"] = {
            key: candidate.get(key)
            for key in (
                "source_type",
                "source_uid",
                "external_id",
                "url",
                "published_at",
                "source_updated_at",
            )
        }
        result["title"] = candidate.get("title", "")
        result["summary"] = candidate.get("description", "")
        excerpt = result.pop("content_excerpt", None)
        result["content_excerpt"] = (
            {
                "text": excerpt[:4000],
                "source_url": result["source"]["url"],
                "truncated": len(excerpt) >= 4000
                or any("truncated" in value.lower() for value in result["limitations"]),
                "trust": "untrusted_external",
            }
            if excerpt
            else None
        )
        result["limitations"] = result.pop("limitations", [])
        return result

    def _adapter_for_target(self, target: str) -> Any:
        for feed in self.adapters:
            capabilities = self.adapters.capabilities(feed)
            if (
                capabilities.supports_inspect
                and capabilities.target_matcher is not None
                and capabilities.target_matcher(target)
            ):
                return self.adapters[feed]
        raise InfoDeskError("UNSUPPORTED_SOURCE", "Target is not a supported source URL or ID.")
