"""Supported research and open-source metadata sources."""

from .arxiv import ArxivAdapter
from .base import AdapterError, CandidateSource, PageResult, SourceAdapter, SourceInspection
from .github import GitHubAdapter
from .hf_models import HuggingFaceModelsAdapter
from .hf_papers import HuggingFacePapersAdapter

__all__ = [
    "AdapterError",
    "ArxivAdapter",
    "CandidateSource",
    "GitHubAdapter",
    "HuggingFaceModelsAdapter",
    "HuggingFacePapersAdapter",
    "PageResult",
    "SourceAdapter",
    "SourceInspection",
]
