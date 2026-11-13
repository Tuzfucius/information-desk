"""GitHub repository search and metadata adapter."""

from __future__ import annotations

import base64
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

from .base import (
    AdapterError,
    CandidateSource,
    PageResult,
    SourceInspection,
    bounded_get,
    clean_text,
    now_utc,
    parse_window_token,
    utc_iso,
)

API = "https://api.github.com"
CODE_EXTENSIONS = re.compile(
    r"\.(?:py|pyi|ipynb|js|ts|tsx|jsx|rs|go|java|cpp|c|h|hpp|sh|yaml|yml|toml)$",
    re.IGNORECASE,
)
BUILD_FILES = {
    "makefile",
    "dockerfile",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "cargo.toml",
    "go.mod",
    "requirements.txt",
    "environment.yml",
    "environment.yaml",
    "cmakelists.txt",
    "build.gradle",
    "pom.xml",
}
README_LIMIT = 4000
MAX_RESPONSE_BYTES = 4_000_000
LINK_RE = re.compile(r"https?://(?:arxiv\.org|huggingface\.co|github\.com)/[^\s<>\])\"']+", re.IGNORECASE)


class GitHubAdapter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str | None = None,
        page_size: int = 100,
    ):
        self.client = client
        self.token = token
        self.page_size = max(1, min(page_size, 100))

    def normalize(self, raw: dict[str, Any]) -> CandidateSource:
        repo_id = str(raw.get("full_name") or raw.get("name") or "").strip()
        stable_id = raw.get("id")
        if not repo_id or stable_id is None:
            raise AdapterError("UPSTREAM_UNAVAILABLE", "GitHub repository has no stable numeric ID.")
        owner_name = repo_id
        if "/" not in owner_name:
            owner = raw.get("owner") or {}
            owner_name = f"{owner.get('login', '')}/{repo_id}".strip("/")
        license_data = raw.get("license")
        return CandidateSource(
            source_type="github_repo",
            source_uid=str(stable_id),
            external_id=owner_name,
            url=f"https://github.com/{owner_name}",
            title=owner_name,
            description=clean_text(raw.get("description"), 800),
            published_at=utc_iso(raw.get("created_at")),
            source_updated_at=utc_iso(raw.get("pushed_at") or raw.get("updated_at")),
            metadata={
                "github_id": int(stable_id),
                "default_branch": raw.get("default_branch"),
                "created_at": utc_iso(raw.get("created_at")),
                "updated_at": utc_iso(raw.get("updated_at")),
                "pushed_at": utc_iso(raw.get("pushed_at")),
                "stargazers_count": raw.get("stargazers_count"),
                "fork": bool(raw.get("fork", False)),
                "archived": bool(raw.get("archived", False)),
                "empty": int(raw.get("size", 0) or 0) == 0,
                "license": license_data.get("spdx_id") if isinstance(license_data, dict) else None,
                "topics": raw.get("topics", []),
                "homepage": raw.get("homepage") or None,
            },
            extracted_links=[],
        )

    async def discover(
        self,
        query: dict[str, Any],
        window: tuple[datetime, datetime],
        page_token: str | None = None,
    ) -> PageResult:
        page = parse_window_token(page_token, default=1)
        if page < 1:
            raise AdapterError("INVALID_CURSOR", "Invalid GitHub page token.")
        start, end = window
        start_day = _utc_date(start)
        end_day = _utc_date(end)
        mode = str(query.get("mode", "created"))
        qualifier = "pushed" if mode in {"updated", "recently_updated"} else "created"
        search_terms = _search_terms(query)
        search_query = " ".join(search_terms + [f"{qualifier}:{start_day}..{end_day}"])
        if len(search_query) > 256:
            raise AdapterError("INVALID_ARGUMENT", "GitHub search query exceeds 256 characters.")
        response = await bounded_get(
            self.client,
            f"{API}/search/repositories",
            params={
                "q": search_query,
                "sort": "updated" if qualifier == "pushed" else "created",
                "order": "desc",
                "per_page": self.page_size,
                "page": page,
            },
            headers=self._headers(),
        )
        if response.status_code == 403:
            _raise_github_forbidden(response)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise AdapterError("UPSTREAM_UNAVAILABLE", "GitHub search returned an invalid response.")
        total_count = int(payload.get("total_count", 0))
        records = [
            self.normalize(repo)
            for repo in payload["items"]
            if isinstance(repo, dict)
        ]
        next_page = page + 1
        capped_at_search_limit = page * self.page_size >= 1000 and total_count >= 1000
        incomplete = bool(payload.get("incomplete_results")) or capped_at_search_limit
        page_complete = next_page * self.page_size > total_count or page * self.page_size >= min(total_count, 1000)
        if incomplete and not page_complete:
            page_complete = False
        warnings = []
        if payload.get("incomplete_results"):
            warnings.append("GITHUB_INCOMPLETE_RESULTS")
        if capped_at_search_limit:
            warnings.append("GITHUB_SEARCH_LIMIT")
        qualifier_date = "pushed_at" if qualifier == "pushed" else "published_at"
        records = [
            record for record in records
            if _in_window(
                record.metadata.get("pushed_at") if qualifier_date == "pushed_at" else record.published_at,
                start,
                end,
            )
        ]
        return PageResult(
            records=records,
            next_page_token=str(next_page) if not page_complete else None,
            page_complete=page_complete and not incomplete,
            source_response_incomplete=incomplete,
            warnings=warnings,
        )

    async def inspect(self, target: str) -> SourceInspection:
        owner, repo = _repo_path(target)
        repo_path = f"{quote(owner, safe='')}/{quote(repo, safe='')}"
        response = await bounded_get(self.client, f"{API}/repos/{repo_path}", headers=self._headers())
        if response.status_code == 404:
            raise AdapterError("NOT_FOUND", "GitHub repository was not found or is not public.")
        if response.status_code == 403:
            _raise_github_forbidden(response)
        response.raise_for_status()
        raw = response.json()
        source = self.normalize(raw)
        limitations: list[str] = []
        signals: list[str] = []
        root_files: list[str] = []
        excerpt: str | None = None
        code_status = "unknown"
        contents = await bounded_get(
            self.client,
            f"{API}/repos/{repo_path}/contents",
            headers=self._headers(),
            params={"ref": raw.get("default_branch") or "HEAD"},
        )
        if contents.status_code == 403:
            limitations.append("GitHub denied access to the repository contents.")
        elif contents.status_code == 404:
            limitations.append("Repository root contents are unavailable.")
        else:
            contents.raise_for_status()
            if len(contents.content) > MAX_RESPONSE_BYTES:
                limitations.append("Repository root response exceeded the inspection limit.")
            else:
                payload = contents.json()
                if isinstance(payload, list):
                    root_files = [
                        str(entry.get("name", ""))
                        for entry in payload
                        if isinstance(entry, dict) and entry.get("name")
                    ][:200]
                    code_files = [
                        name for name in root_files
                        if CODE_EXTENSIONS.search(name) or name.lower() in BUILD_FILES
                    ]
                    if code_files:
                        code_status = "detected"
                        signals.extend(f"file:{name}" for name in code_files[:10])
                    else:
                        code_status = "not_detected"
                    readme_entry = next(
                        (entry for entry in payload if isinstance(entry, dict) and str(entry.get("name", "")).lower() == "readme.md"),
                        None,
                    )
                    if readme_entry:
                        excerpt, readme_links, readme_warnings = await self._read_readme(repo_path, readme_entry)
                        limitations.extend(readme_warnings)
                    else:
                        readme_links = []
                else:
                    limitations.append("GitHub did not return a repository root file list.")
                    readme_links = []
        return SourceInspection(
            source=source,
            checked_at=now_utc(),
            code_status=code_status,
            signals=signals,
            root_files=root_files,
            checked_depth=1 if root_files else 0,
            extracted_links=readme_links if "readme_links" in locals() else [],
            content_excerpt=excerpt,
            limitations=limitations,
        )

    async def _read_readme(
        self,
        repo_path: str,
        entry: dict[str, Any],
    ) -> tuple[str | None, list[dict[str, Any]], list[str]]:
        limitations: list[str] = []
        links: list[dict[str, Any]] = []
        path = str(entry.get("path") or "README.md")
        response = await bounded_get(
            self.client,
            f"{API}/repos/{repo_path}/contents/{quote(path, safe='/')}",
            headers=self._headers(),
            max_bytes=MAX_RESPONSE_BYTES,
        )
        if response.status_code in {401, 403}:
            return None, links, ["README is inaccessible to this client."]
        if response.status_code == 404:
            return None, links, ["README was not found."]
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_BYTES:
            return None, links, ["README response exceeded the inspection limit."]
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("encoding") != "base64" or not payload.get("content"):
            return None, links, ["README content was not returned inline."]
        try:
            content = base64.b64decode(payload["content"], validate=False).decode("utf-8", errors="replace")
        except (ValueError, UnicodeError):
            return None, links, ["README could not be decoded."]
        if len(content) > README_LIMIT:
            limitations.append("README excerpt truncated to 4000 characters.")
        for url in list(dict.fromkeys(LINK_RE.findall(content)))[:30]:
            links.append({
                "url": url.rstrip(".,;"),
                "relation_type": "references",
                "evidence_level": "weak",
                "evidence_origin": "github_readme",
            })
        return content[:README_LIMIT], links, limitations

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


def _repo_path(target: str) -> tuple[str, str]:
    parsed = urlparse(target if "://" in target else f"https://github.com/{target}")
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"} or parsed.port is not None:
        raise AdapterError("INVALID_TARGET", "Only GitHub repository identifiers and HTTPS URLs are supported.")
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2 or parts[0].startswith("@") or parts[1].endswith((".git",)):
        if len(parts) == 2 and parts[1].lower().endswith(".git"):
            parts[1] = parts[1][:-4]
        elif len(parts) >= 3 and parts[2] in {"tree", "blob", "issues", "pull", "discussions", "actions"}:
            raise AdapterError("INVALID_TARGET", "Target must identify a GitHub repository root.")
        else:
            raise AdapterError("INVALID_TARGET", "Invalid GitHub repository URL.")
    if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
        raise AdapterError("INVALID_TARGET", "Invalid GitHub repository URL.")
    return parts[0], parts[1]


def _search_terms(query: dict[str, Any]) -> list[str]:
    raw = query.get("search") or query.get("topic") or query.get("topics") or []
    if isinstance(raw, str):
        topics = [raw]
    elif isinstance(raw, list):
        topics = [str(value) for value in raw[:5]]
    else:
        topics = []
    clean = [re.sub(r"[^A-Za-z0-9_.+-]", " ", topic).strip() for topic in topics]
    clean = [topic for topic in clean if topic]
    if not clean:
        return []
    if len(clean) == 1:
        return [clean[0]]
    return ["(" + " OR ".join(clean) + ")"]


def _utc_date(value: datetime) -> str:
    from datetime import UTC
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.date().isoformat()


def _in_window(value: str | None, start: datetime, end: datetime) -> bool:
    if not value:
        return False
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    from datetime import UTC

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return start.astimezone(UTC) <= parsed.astimezone(UTC) < end.astimezone(UTC)


def _raise_github_forbidden(response: httpx.Response) -> None:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        raise AdapterError("RATE_LIMITED", f"GitHub rate limit reached. Retry after {retry_after} seconds.")
    remaining = response.headers.get("X-RateLimit-Remaining")
    if remaining == "0":
        reset = response.headers.get("X-RateLimit-Reset")
        raise AdapterError("RATE_LIMITED", f"GitHub rate limit reached. Reset at {reset or 'an unknown time'}.")
    raise AdapterError("UPSTREAM_FORBIDDEN", "GitHub denied access to the requested resource.")
