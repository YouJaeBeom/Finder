"""Cross-source paper deduplication using multi-identifier matching."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from .models import Paper, PaperIdentifiers

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

    def is_seen(self, paper: Paper) -> bool:
        """Return True if this paper is already in the dedup store."""
        pid = paper.identifiers
        if pid.arxiv_id and pid.arxiv_id in self._arxiv_ids:
            return True
        if pid.doi and pid.doi in self._dois:
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


def deduplicate_collected(papers: List[Paper]) -> List[Paper]:
    """Within a single run, deduplicate papers from multiple sources.

    A paper that appears in both arXiv and OpenAlex is merged into one entry,
    preserving sources from both.
    """
    seen: Dict[str, Paper] = {}  # normalized_title -> Paper

    for paper in papers:
        key = paper.identifiers.normalized_title

        # Try arxiv_id match first
        if paper.identifiers.arxiv_id:
            for existing in seen.values():
                if existing.identifiers.arxiv_id == paper.identifiers.arxiv_id:
                    # Merge sources
                    for src in paper.source:
                        if src not in existing.source:
                            existing.source.append(src)
                    # Prefer the richer abstract
                    if not existing.abstract and paper.abstract:
                        existing.abstract = paper.abstract
                    break
            else:
                seen[key] = paper
            continue

        # Try DOI match
        if paper.identifiers.doi:
            for existing in seen.values():
                if existing.identifiers.doi == paper.identifiers.doi:
                    for src in paper.source:
                        if src not in existing.source:
                            existing.source.append(src)
                    if not existing.abstract and paper.abstract:
                        existing.abstract = paper.abstract
                    break
            else:
                seen[key] = paper
            continue

        # Fall back to normalized title match
        if key in seen:
            for src in paper.source:
                if src not in seen[key].source:
                    seen[key].source.append(src)
            if not seen[key].abstract and paper.abstract:
                seen[key].abstract = paper.abstract
        else:
            seen[key] = paper

    return list(seen.values())
