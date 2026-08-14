"""Cross-source paper deduplication using multi-identifier matching."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from .models import Paper

logger = logging.getLogger(__name__)

STATE_FILE = "seen_ids.json"


def _load_seen(path: str = STATE_FILE) -> List[dict]:
    """Load existing seen-paper records from the state file."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s — treating as empty", path, exc)
        return []


def _save_seen(records: List[dict], path: str = STATE_FILE) -> None:
    """Persist seen-paper records to the state file."""
    Path(path).write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _make_record(paper: Paper) -> dict:
    """Create a persistable record for a paper."""
    return {
        "arxiv_id": paper.identifiers.arxiv_id,
        "doi": paper.identifiers.doi,
        "normalized_title": paper.identifiers.normalized_title,
        "url": paper.identifiers.url,
        "content_type": paper.content_type,
        "notion_page_id": paper.notion_page_id,
        "title": paper.title,
    }


def _matches(paper: Paper, record: dict) -> bool:
    """Return True if any identifier field matches between paper and record."""
    pid = paper.identifiers

    if pid.arxiv_id and record.get("arxiv_id") and pid.arxiv_id == record["arxiv_id"]:
        return True

    if pid.doi and record.get("doi") and pid.doi == record["doi"]:
        return True

    # News items carry no arXiv ID or DOI — the link is their identity.
    if pid.url and record.get("url") and pid.url == record["url"]:
        return True

    if (
        pid.normalized_title
        and record.get("normalized_title")
        and pid.normalized_title == record["normalized_title"]
    ):
        return True

    return False


class DedupStore:
    """Tracks collected papers to prevent cross-run duplicates."""

    def __init__(self, path: str = STATE_FILE) -> None:
        self.path = path
        self._records: List[dict] = _load_seen(path)
        # Build lookup sets for O(1) matching
        self._arxiv_ids: set = {r["arxiv_id"] for r in self._records if r.get("arxiv_id")}
        self._dois: set = {r["doi"] for r in self._records if r.get("doi")}
        self._titles: set = {
            r["normalized_title"] for r in self._records if r.get("normalized_title")
        }
        self._urls: set = {r["url"] for r in self._records if r.get("url")}

    def is_seen(self, paper: Paper) -> bool:
        """Return True if this paper is already in the dedup store."""
        pid = paper.identifiers
        if pid.arxiv_id and pid.arxiv_id in self._arxiv_ids:
            return True
        if pid.doi and pid.doi in self._dois:
            return True
        if pid.url and pid.url in self._urls:
            return True
        if pid.normalized_title and pid.normalized_title in self._titles:
            return True
        return False

    def mark_seen(self, paper: Paper) -> None:
        """Add a paper to the in-memory store (call persist() to save)."""
        pid = paper.identifiers
        if pid.arxiv_id:
            self._arxiv_ids.add(pid.arxiv_id)
        if pid.doi:
            self._dois.add(pid.doi)
        if pid.url:
            self._urls.add(pid.url)
        if pid.normalized_title:
            self._titles.add(pid.normalized_title)
        self._records.append(_make_record(paper))

    def get_record(self, paper: Paper) -> Optional[dict]:
        """Return the stored record for a paper if it exists, else None."""
        for record in self._records:
            if _matches(paper, record):
                return record
        return None

    def persist(self) -> None:
        """Write the current state to disk."""
        _save_seen(self._records, self.path)


def _merge_into(existing: Paper, duplicate: Paper) -> None:
    """Fold a duplicate into the entry already kept, preferring richer data."""
    for src in duplicate.source:
        if src not in existing.source:
            existing.source.append(src)
    if not existing.abstract and duplicate.abstract:
        existing.abstract = duplicate.abstract
    # Keep the highest community score seen for the story — the same article
    # posted twice to Hacker News should carry the score that stuck.
    if duplicate.points is not None and (existing.points or 0) < duplicate.points:
        existing.points = duplicate.points


_IDENTITY_FIELDS = ("arxiv_id", "doi", "url", "normalized_title")


def deduplicate_collected(papers: List[Paper]) -> List[Paper]:
    """Within a single run, deduplicate items collected from multiple sources.

    A paper appearing in both arXiv and OpenAlex is merged into one entry, as is
    a news story carried by two feeds. Identity follows the same rule as the
    cross-run store: any single match on arXiv ID, DOI, URL or normalized title
    means the same item.
    """
    unique: List[Paper] = []
    index: Dict[tuple, Paper] = {}  # (field, value) -> the entry we kept

    for paper in papers:
        keys = [
            (field, value)
            for field in _IDENTITY_FIELDS
            if (value := getattr(paper.identifiers, field))
        ]

        kept = next((index[key] for key in keys if key in index), None)
        if kept is None:
            unique.append(paper)
            kept = paper
        else:
            _merge_into(kept, paper)

        # Register every identifier this item carries, so a later item sharing
        # any one of them — a different headline on the same link, say — resolves
        # to the entry we already kept.
        for key in keys:
            index.setdefault(key, kept)

    return unique
