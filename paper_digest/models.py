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
    """Composite identity for cross-source deduplication.

    Any single field matching means the same item. News items carry no arXiv ID
    or DOI, so *url* is their primary key.
    """

    arxiv_id: Optional[str] = None  # e.g. "2401.12345" (no version suffix)
    doi: Optional[str] = None  # e.g. "10.18653/v1/2024.acl-long.1"
    normalized_title: str = ""  # lowercase, stripped
    url: Optional[str] = None  # canonical link; primary identity for news


@dataclass
class ResearchNote:
    """LLM-generated Korean note.

    Papers get four sections; news items get three — "방법" (method) does not
    apply to a news article, so it stays empty and is not counted as missing.
    """

    one_line_summary: str = ""
    key_contributions: List[str] = field(default_factory=list)
    method: str = ""
    relevance_to_profile: str = ""
    content_type: str = "paper"  # "paper" | "news"

    def expected_sections(self) -> int:
        """How many sections this note type is supposed to have."""
        return 3 if self.content_type == "news" else 4

    def sections_filled_count(self) -> int:
        """Count non-empty sections that apply to this note's type."""
        count = 0
        if self.one_line_summary.strip():
            count += 1
        if self.key_contributions:
            count += 1
        if self.content_type != "news" and self.method.strip():
            count += 1
        if self.relevance_to_profile.strip():
            count += 1
        return count

    def is_complete(self) -> bool:
        """True when every section that applies to this type is populated."""
        return self.sections_filled_count() == self.expected_sections()


@dataclass
class Paper:
    """A collected item — a research paper, or an IT news story.

    One model covers both so the dedup / ranking / note / Notion stages stay
    shared. *content_type* is what diverges the behaviour: papers require an
    abstract to be rankable and get a four-section note; news items rank on
    title plus summary and get three sections.
    """

    identifiers: PaperIdentifiers
    title: str
    abstract: Optional[str]
    authors: List[str] = field(default_factory=list)
    venue: str = "arXiv preprint"  # news: the site or feed name
    venue_status: str = "preprint"  # "preprint" | "accepted" | "published"
    collection_date: str = ""
    source: List[str] = field(default_factory=list)  # arxiv|openalex|hackernews|rss
    matched_keywords: List[str] = field(default_factory=list)
    content_type: str = "paper"  # "paper" | "news"
    url: Optional[str] = None  # article/story link, shown in the Notion header

    # News ordering signals. News skips LLM relevance scoring, so these are what
    # decide which stories make the top_n cut.
    points: Optional[int] = None  # Hacker News community score; None for RSS
    published_at: Optional[str] = None  # ISO 8601 publish time when the source gives one

    # Populated after ranking (papers only — news keeps 0.0)
    relevance_score: float = 0.0

    # Populated after note generation
    research_note: Optional[ResearchNote] = None

    # Populated after Notion write
    notion_page_id: Optional[str] = None
