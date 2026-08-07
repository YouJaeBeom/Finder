"""Domain models for paper digest pipeline."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


def normalize_title(title: str) -> str:
    """Normalize a paper title for deduplication.

    Lowercases, strips punctuation, and collapses whitespace so that minor
    formatting differences don't create false duplicates.
    """
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[^\w\s]", "", t)  # remove punctuation
    t = re.sub(r"\s+", " ", t).strip()
    return t


@dataclass
class PaperIdentifiers:
    """Composite paper identity for cross-source deduplication."""

    arxiv_id: Optional[str] = None  # e.g. "2401.12345" (no version suffix)
    doi: Optional[str] = None  # e.g. "10.18653/v1/2024.acl-long.1"
    normalized_title: str = ""  # lowercase, stripped


@dataclass
class ResearchNote:
    """LLM-generated Korean research note with four sections."""

    one_line_summary: str = ""
    key_contributions: List[str] = field(default_factory=list)
    method: str = ""
    relevance_to_profile: str = ""

    def sections_filled_count(self) -> int:
        """Count how many of the 4 sections have non-empty content."""
        count = 0
        if self.one_line_summary.strip():
            count += 1
        if self.key_contributions:
            count += 1
        if self.method.strip():
            count += 1
        if self.relevance_to_profile.strip():
            count += 1
        return count


@dataclass
class Paper:
    """A research paper collected from arXiv or OpenAlex."""

    identifiers: PaperIdentifiers
    title: str
    abstract: Optional[str]
    authors: List[str] = field(default_factory=list)
    venue: str = "arXiv preprint"
    venue_status: str = "preprint"  # "preprint" | "accepted"
    collection_date: str = ""
    source: List[str] = field(default_factory=list)  # "arxiv" | "openalex"
    matched_keywords: List[str] = field(default_factory=list)

    # Populated after ranking
    relevance_score: float = 0.0

    # Populated after note generation
    research_note: Optional[ResearchNote] = None

    # Populated after Notion write
    notion_page_id: Optional[str] = None
