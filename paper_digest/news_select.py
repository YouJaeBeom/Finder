"""News selection — which stories get a Korean note, decided without an LLM.

Papers need the cheap-model relevance gate because the venue allowlist admits
every field its venues publish, and a query can match a paper that has nothing
to do with the research profile. News does not have that problem: Hacker News is
already filtered by community score, and the RSS feeds are hand-picked by the
user. Almost everything collected is on-topic by construction, so paying a
model to re-confirm relevance buys nothing and costs a call per story.

Selection is therefore mechanical:

    query filter (optional) → order within each source → round-robin → top_n

The round-robin matters. A busy feed like TechCrunch publishes ~20 items a day
and would otherwise take every slot in the digest, burying the Hacker News
stories the score filter already vouched for. Taking turns across sources keeps
the monthly digest mixed.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .models import Paper
from .query import Query, filter_papers

logger = logging.getLogger(__name__)


def _order_within_source(items: List[Paper]) -> List[Paper]:
    """Best-first within one source: community score, then recency.

    RSS items carry no score, so for a feed this collapses to newest-first.
    """
    return sorted(
        items,
        key=lambda item: (item.points or 0, item.published_at or ""),
        reverse=True,
    )


def _group_by_source(items: List[Paper]) -> List[List[Paper]]:
    """Group items by their venue, preserving first-seen order.

    *venue* is the publication ("Hacker News", "TechCrunch"), which is the unit
    a reader would want variety across — grouping by ``source`` instead would
    lump every RSS feed into a single bucket.
    """
    groups: Dict[str, List[Paper]] = {}
    for item in items:
        groups.setdefault(item.venue or "unknown", []).append(item)
    return [_order_within_source(group) for group in groups.values()]


def _round_robin(groups: List[List[Paper]], limit: int) -> List[Paper]:
    """Take one item from each source in turn until *limit* is reached."""
    selected: List[Paper] = []
    depth = 0
    while len(selected) < limit and any(len(group) > depth for group in groups):
        for group in groups:
            if depth < len(group):
                selected.append(group[depth])
                if len(selected) == limit:
                    return selected
        depth += 1
    return selected


def select_news(
    items: List[Paper],
    query: Optional[Query],
    top_n: int,
) -> List[Paper]:
    """Pick up to *top_n* news items to write up, with no LLM involved.

    No *query* means "keep everything" — the sources are already curated, so a
    lab that wants the whole firehose summarised just leaves the key out rather
    than editing code.
    """
    if top_n <= 0 or not items:
        return []

    candidates = filter_papers(items, query) if query else list(items)
    if not candidates:
        logger.info("News: %d collected, none matched news.query", len(items))
        return []

    selected = _round_robin(_group_by_source(candidates), top_n)
    logger.info(
        "News: %d collected → %d after news.query → %d selected (%s)",
        len(items),
        len(candidates),
        len(selected),
        ", ".join(sorted({item.venue for item in selected})) or "none",
    )
    return selected
