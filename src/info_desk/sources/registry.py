"""Explicit adapter registrations; no dynamic plugin loading is performed."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from math import isfinite
from typing import Any
from urllib.parse import urlparse

from .base import SourceCapabilities

FEED_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _valid_host(host: str) -> bool:
    if len(host) > 253 or not host.isascii():
        return False
    return all(
        label
        and len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in label)
        for label in host.split(".")
    )


def _arxiv_target(target: str) -> bool:
    if "://" not in target:
        return bool(re.fullmatch(
            r"(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?", target, re.IGNORECASE
        ))
    parsed = urlparse(target)
    return parsed.scheme == "https" and parsed.hostname in {"arxiv.org", "export.arxiv.org"}


def _github_target(target: str) -> bool:
    parsed = urlparse(target)
    return parsed.scheme == "https" and parsed.hostname in {"github.com", "www.github.com"}


def _hf_paper_target(target: str) -> bool:
    parsed = urlparse(target)
    return parsed.scheme == "https" and parsed.hostname == "huggingface.co" and parsed.path.startswith("/papers/")


def _hf_model_target(target: str) -> bool:
    parsed = urlparse(target)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    return (parsed.scheme == "https" and parsed.hostname == "huggingface.co"
            and len(parts) == 2 and parts[0] not in {"papers", "datasets", "spaces", "collections"})


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    feed: str
    adapter: Any
    capabilities: SourceCapabilities


class AdapterRegistry(Mapping[str, Any]):
    """A small, explicit registry shared by ingestion, inspection and Watch."""

    def __init__(self, registrations: list[AdapterRegistration] | None = None) -> None:
        self._registrations: dict[str, AdapterRegistration] = {}
        for registration in registrations or []:
            self.register(registration)

    def register(self, registration: AdapterRegistration) -> None:
        if not FEED_NAME.fullmatch(registration.feed):
            raise ValueError(f"invalid feed name: {registration.feed!r}")
        if registration.feed in self._registrations:
            raise ValueError(f"feed already registered: {registration.feed}")
        if not registration.capabilities.source_types or any(
            not FEED_NAME.fullmatch(source_type)
            for source_type in registration.capabilities.source_types
        ):
            raise ValueError("an adapter must declare valid source type identifiers")
        capabilities = registration.capabilities
        if capabilities.min_request_interval_seconds < 0 or not isfinite(
            capabilities.min_request_interval_seconds
        ):
            raise ValueError("minimum request interval must be a finite nonnegative value")
        if any(
            not _valid_host(host)
            for host in capabilities.request_hosts
        ):
            raise ValueError("request hosts must be explicit DNS hostnames")
        self._registrations[registration.feed] = registration

    def __getitem__(self, feed: str) -> Any:
        return self._registrations[feed].adapter

    def __iter__(self) -> Iterator[str]:
        return iter(self._registrations)

    def __len__(self) -> int:
        return len(self._registrations)

    def registration(self, feed: str) -> AdapterRegistration:
        return self._registrations[feed]

    def capabilities(self, feed: str) -> SourceCapabilities:
        return self.registration(feed).capabilities

    def feed_for_source_type(self, source_type: str) -> str | None:
        matches = [
            feed for feed in self
            if source_type in self.capabilities(feed).source_types
        ]
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def from_mapping(cls, adapters: Mapping[str, Any]) -> AdapterRegistry:
        """Compatibility bridge for existing callers; new adapters should register explicitly."""
        defaults = {
            "arxiv": SourceCapabilities(
                ("arxiv",), window_resolution_seconds=86_400,
                title_priorities={"arxiv": 100},
                target_matcher=_arxiv_target,
                query_builder=lambda topics: [{"topics": topics, "categories": [
                    "cs.AI", "cs.CL", "cs.LG", "cs.CV", "cs.RO"
                ]}],
            ),
            "hf_papers": SourceCapabilities(
                ("hf_paper",), window_resolution_seconds=86_400,
                title_priorities={"hf_paper": 90}, target_matcher=_hf_paper_target,
                query_builder=lambda _topics: [{}],
            ),
            "hf_models": SourceCapabilities(
                ("hf_model",), window_resolution_seconds=None,
                window_filtering="client", durable_cursor=False,
                watch_interval_hours=12, title_priorities={"hf_model": 80},
                target_matcher=_hf_model_target,
                query_builder=lambda topics: [{"topic": topic} for topic in topics],
            ),
            "github": SourceCapabilities(
                ("github_repo",), window_resolution_seconds=1,
                title_priorities={"github_repo": 60}, watch_interval_hours=6,
                target_matcher=_github_target,
                query_builder=lambda topics: [
                    {"topic": topic, "mode": mode}
                    for topic in topics for mode in ("created", "updated")
                ],
            ),
        }
        registrations = []
        for feed, adapter in adapters.items():
            declared = getattr(adapter, "capabilities", None)
            fallback = defaults.get(feed)
            capabilities = declared or fallback
            if declared is not None and fallback is not None:
                capabilities = replace(
                    declared,
                    query_builder=declared.query_builder or fallback.query_builder,
                    target_matcher=declared.target_matcher or fallback.target_matcher,
                    title_priorities=declared.title_priorities or fallback.title_priorities,
                )
            if capabilities is None:
                raise ValueError(f"adapter {feed!r} must declare SourceCapabilities")
            registrations.append(AdapterRegistration(feed, adapter, capabilities))
        return cls(registrations)


def create_default_registry(http_client: Any, github_token: str | None = None) -> AdapterRegistry:
    from .arxiv import ArxivAdapter
    from .github import GitHubAdapter
    from .hf_models import HuggingFaceModelsAdapter
    from .hf_papers import HuggingFacePapersAdapter

    return AdapterRegistry.from_mapping({
        "arxiv": ArxivAdapter(http_client),
        "hf_papers": HuggingFacePapersAdapter(http_client),
        "hf_models": HuggingFaceModelsAdapter(http_client),
        "github": GitHubAdapter(http_client, token=github_token),
    })
