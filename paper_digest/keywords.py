"""Keyword matching shared by the paper and news stages.

Lives in its own module because both the paper pipeline and the LLM-free news
selection need it, and news importing it from ``pipeline`` would be circular.
"""
from __future__ import annotations

from typing import List

from .models import Paper


def matches_keywords(paper: Paper, keywords: List[str]) -> List[str]:
    """Return the keywords that appear in the item's title or body text."""
    text = f"{paper.title} {paper.abstract or ''}".lower()
    return [kw for kw in keywords if kw.lower() in text]


def filter_by_keywords(papers: List[Paper], keywords: List[str]) -> List[Paper]:
    """Keep items matching at least one keyword, recording which ones matched.

    An empty *keywords* list matches nothing, which is the literal reading and
    keeps the paper pipeline safe: papers must never bypass the keyword gate,
    since collection is scoped by those same keywords. The news stage treats an
    empty list as "keep everything" and skips this call entirely — that choice
    belongs to the caller, not here.
    """
    result: List[Paper] = []
    for paper in papers:
        matched = matches_keywords(paper, keywords)
        if matched:
            paper.matched_keywords = matched
            result.append(paper)
    return result
