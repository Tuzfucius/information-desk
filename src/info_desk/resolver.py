"""Deterministic source identity, evidence, and event resolution."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

from info_desk.sources.base import CandidateSource
from info_desk.storage.repository import Repository


class IdentityResolver:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def ingest_candidate(
        self,
        candidate: CandidateSource,
        *,
        conn: Any,
        now: str,
        run_id: int | None,
        triage_status: str = "inbox",
        category: str = "general",
        is_watched: bool = False,
        notes: str = "",
        last_checked_at: str | None = None,
    ) -> tuple[int, int, bool, bool, int, int]:
        candidate_data = candidate.to_dict()
        if last_checked_at is not None:
            candidate_data["last_checked_at"] = last_checked_at
        existing = self.repository.get_source(
            candidate.source_type, candidate.source_uid, conn=conn
        )
        links = self.normalized_links(candidate)
        strong_links = [link for link in links if link["evidence_level"] == "strong"]
        matched = self.repository.find_items_for_source_links(strong_links, conn=conn)
        item_ids = {int(row["item_id"]) for row in matched}
        same_paper_type = (
            "hf_paper" if candidate.source_type == "arxiv" else
            "arxiv" if candidate.source_type == "hf_paper" else None
        )
        if same_paper_type:
            same_paper = self.repository.get_source(
                same_paper_type, candidate.source_uid, conn=conn
            )
            if same_paper:
                item_ids.add(int(same_paper["item_id"]))

        if existing:
            item_id = int(existing["item_id"])
        elif len(item_ids) == 1:
            item_id = next(iter(item_ids))
        else:
            item_id = self.repository.create_item(
                {
                    "title": candidate.title,
                    "summary": candidate.description,
                    "category": category,
                    "triage_status": triage_status,
                    "is_watched": is_watched,
                    "notes": notes,
                },
                now=now,
                conn=conn,
            )

        item_created = existing is None and len(item_ids) != 1
        source_id, source_created = self.repository.upsert_source(
            item_id, candidate_data, now=now, conn=conn
        )
        if links:
            self.repository.set_source_links(
                source_id, links, observed_at=now, conn=conn
            )

        event_count = 0
        conflict_count = 0
        fingerprint = self.content_fingerprint(candidate)
        if existing is None:
            event_type = "discovered" if item_created else "source_attached"
            payload = {"title": candidate.title}
            key = self.event_key(event_type, candidate, fingerprint)
            _, created = self.repository.create_event(
                item_id,
                source_id,
                event_type,
                key,
                payload,
                created_at=now,
                run_id=run_id,
                conn=conn,
            )
            event_count += int(created)
            if same_paper_type and item_id and candidate.source_type in {"arxiv", "hf_paper"}:
                self.repository.set_source_links(
                    source_id,
                    [
                        {
                            "target_source_type": same_paper_type,
                            "target_external_id": candidate.source_uid,
                            "target_url": f"https://{'huggingface.co/papers' if same_paper_type == 'hf_paper' else 'arxiv.org/abs'}/{candidate.source_uid}",
                            "relation_type": "same_paper",
                            "evidence_level": "strong",
                            "evidence_origin": "shared_arxiv_id",
                            "evidence_excerpt": candidate.source_uid,
                        }
                    ],
                    observed_at=now,
                    conn=conn,
                )
        else:
            previous_fingerprint = self.stored_content_fingerprint(existing)
            if previous_fingerprint and previous_fingerprint != fingerprint:
                key = self.event_key("content_changed", candidate, fingerprint)
                _, created = self.repository.create_event(
                    item_id,
                    source_id,
                    "content_changed",
                    key,
                    {"changed_fields": ["title", "description"]},
                    created_at=now,
                    run_id=run_id,
                    conn=conn,
                )
                event_count += int(created)
        for target_id in sorted(item_ids):
            if target_id == item_id:
                continue
            self.repository.add_merge_conflict(
                target_id,
                item_id,
                {"source": candidate_data, "strong_links": strong_links},
                created_at=now,
                conn=conn,
            )
            conflict_count += 1
        previous_item = self.repository.get_item(item_id, conn=conn)
        self.repository.set_item_automatic_fields(
            item_id,
            now=now,
            title=candidate.title,
            summary=candidate.description,
            code_status=(
                "detected"
                if candidate.source_type == "github_repo"
                and candidate.metadata.get("code_status") == "detected"
                else None
            ),
            conn=conn,
        )
        if (
            previous_item is not None
            and candidate.source_type == "github_repo"
            and candidate.metadata.get("code_status") == "detected"
            and previous_item["code_status"] != "detected"
        ):
            _, created = self.repository.create_event(
                item_id,
                source_id,
                "code_detected",
                self.event_key("code_detected", candidate, "detected"),
                {
                    "previous_status": previous_item["code_status"],
                    "current_status": "detected",
                    "signals": candidate.metadata.get("code_signals", []),
                },
                created_at=now,
                run_id=run_id,
                conn=conn,
            )
            event_count += int(created)
        return item_id, source_id, source_created, item_created, event_count, conflict_count

    @staticmethod
    def normalized_links(candidate: CandidateSource) -> list[dict[str, Any]]:
        links = []
        for entry in candidate.extracted_links:
            url = entry.get("url")
            if not isinstance(url, str):
                continue
            parsed = urlparse(url)
            source_type = None
            external_id = None
            if parsed.hostname == "github.com":
                parts = [part for part in parsed.path.strip("/").split("/") if part]
                if len(parts) >= 2:
                    source_type = "github_repo"
                    external_id = "/".join(parts[:2]).removesuffix(".git")
            elif parsed.hostname == "huggingface.co":
                parts = [part for part in parsed.path.strip("/").split("/") if part]
                if len(parts) == 2 and parts[0] == "papers":
                    source_type = "hf_paper"
                    external_id = parts[1]
                elif len(parts) == 2 and parts[0] not in {"datasets", "spaces"}:
                    source_type = "hf_model"
                    external_id = "/".join(parts)
            elif parsed.hostname in {"arxiv.org", "export.arxiv.org"}:
                match = re.search(r"(\d{4}\.\d{4,5}|[a-z-]+/\d{7})", parsed.path, re.IGNORECASE)
                if match:
                    source_type = "arxiv"
                    external_id = match.group(1).lower()
            if not source_type or not external_id:
                continue
            links.append(
                {
                    "target_source_type": source_type,
                    "target_external_id": external_id,
                    "target_url": url,
                    "relation_type": entry.get("relation_type", "related"),
                    "evidence_level": entry.get("evidence_level", "weak"),
                    "evidence_origin": entry.get("evidence_origin", candidate.source_type),
                    "evidence_excerpt": entry.get("evidence_excerpt", "")[:500],
                }
            )
        return links

    @staticmethod
    def content_fingerprint(candidate: CandidateSource) -> str:
        content = json.dumps(
            {"title": candidate.title, "description": candidate.description},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(content.encode()).hexdigest()

    @staticmethod
    def stored_content_fingerprint(source: dict[str, Any]) -> str:
        candidate = CandidateSource(
            source_type=source["source_type"],
            source_uid=source["source_uid"],
            external_id=source["external_id"],
            url=source["url"],
            title=source.get("title", ""),
            description=source.get("description", ""),
        )
        return IdentityResolver.content_fingerprint(candidate)

    @staticmethod
    def event_key(event_type: str, candidate: CandidateSource, fingerprint: str) -> str:
        key = f"{event_type}:{candidate.source_type}:{candidate.source_uid}:{fingerprint}"
        return hashlib.sha256(key.encode()).hexdigest()
