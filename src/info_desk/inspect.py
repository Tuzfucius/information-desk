"""Validated routing and normalized result shaping for source inspection."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from info_desk.exceptions import InfoDeskError, UpstreamError
from info_desk.sources.base import AdapterError


class SourceInspector:
    def __init__(self, adapters: dict[str, Any], upstream_error: Any) -> None:
        self.adapters = adapters
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

        result = inspection.to_dict()
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
        raw = target if "://" in target else f"https://arxiv.org/abs/{target}"
        parsed = urlparse(raw)
        host = parsed.hostname
        path = parsed.path.strip("/")
        if host in {"arxiv.org", "export.arxiv.org"}:
            return self.adapters["arxiv"]
        if host in {"github.com", "www.github.com"}:
            return self.adapters["github"]
        if host == "huggingface.co":
            if path.startswith("papers/"):
                return self.adapters["hf_papers"]
            parts = path.split("/")
            if len(parts) == 2 and parts[0] not in {"datasets", "spaces", "collections"}:
                return self.adapters["hf_models"]
        raise InfoDeskError("UNSUPPORTED_SOURCE", "Target is not a supported source URL or ID.")
