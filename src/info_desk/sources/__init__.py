"""Supported research and open-source metadata sources."""

from .arxiv import ArxivAdapter
from .base import (
    AdapterError,
    CandidateSource,
    PageResult,
    SourceAdapter,
    SourceCapabilities,
    SourceInspection,
)
from .github import GitHubAdapter
from .hf_models import HuggingFaceModelsAdapter
from .hf_papers import HuggingFacePapersAdapter
from .registry import AdapterRegistration, AdapterRegistry

__all__ = [
    "AdapterError",
    "AdapterRegistration",
    "AdapterRegistry",
    "ArxivAdapter",
    "CandidateSource",
    "GitHubAdapter",
    "HuggingFaceModelsAdapter",
    "HuggingFacePapersAdapter",
    "PageResult",
    "SourceAdapter",
    "SourceCapabilities",
    "SourceInspection",
]
