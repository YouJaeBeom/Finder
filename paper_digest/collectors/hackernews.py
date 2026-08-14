"""Hacker News collector via the public Firebase API.

No API key and no rate-limit registration — the endpoints are open, which keeps
the "any lab member can fork and run it" constraint intact.

Stories are pre-filtered by community score before anything reaches the LLM:
HN's own points signal is free and a good first-pass relevance filter, so the
ranking model only sees stories the community already surfaced.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

from ..models import Paper, PaperIdentifiers, normalize_title

logger = logging.getLogger(__name__)

API_BASE = "https://hacker-news.firebaseio.com/v0"
TIMEOUT = 20

# HN returns ~500 ranked story IDs. Scanning all of them costs one request each,
# so cap the scan: anything past the top slice is old or low-signal by design.
MAX_STORIES_TO_SCAN = 200


def _fetch_json(url: str) -> Optional[dict | list]:
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # network, JSON, HTTP — all non-fatal for one item
        logger.warning("Hacker News request failed (%s): %s", url, exc)
        return None


def _story_to_paper(story: dict, collection_date: str) -> Optional[Paper]:
    """Convert an HN item payload into a Paper with content_type='news'."""
    title = (story.get("title") or "").strip()
    url = story.get("url")
    if not title or not url:
        # Ask HN / Show HN text posts have no external URL; skip them — there is
        # no article to summarise and the identity key would be missing.
        return None

    posted = story.get("time")
    published_at = (
        datetime.fromtimestamp(posted, tz=timezone.utc).isoformat()
        if isinstance(posted, (int, float))
        else None
    )

    return Paper(
        identifiers=PaperIdentifiers(
            arxiv_id=None,
            doi=None,
            normalized_title=normalize_title(title),
            url=url,
        ),
        title=title,
        # HN carries no article body. The ranking stage treats news as
        # title-rankable, so this stays None rather than being faked.
        abstract=None,
        authors=[story["by"]] if story.get("by") else [],
        venue="Hacker News",
        venue_status="published",
        collection_date=collection_date,
        source=["hackernews"],
        content_type="news",
        url=url,
        # Community score orders the digest now that news skips LLM ranking.
        points=int(story["score"]) if isinstance(story.get("score"), (int, float)) else None,
        published_at=published_at,
    )


def collect_hackernews_stories(
    min_points: int = 100,
    days_back: int = 7,
    max_scan: int = MAX_STORIES_TO_SCAN,
) -> List[Paper]:
    """Fetch top HN stories above *min_points* posted within *days_back* days."""
    ids = _fetch_json(f"{API_BASE}/topstories.json")
    if not isinstance(ids, list):
        logger.error("Hacker News: could not fetch top stories list")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    collection_date = datetime.now(timezone.utc).date().isoformat()
    stories: List[Paper] = []

    for story_id in ids[:max_scan]:
        item = _fetch_json(f"{API_BASE}/item/{story_id}.json")
        if not isinstance(item, dict) or item.get("type") != "story":
            continue

        if (item.get("score") or 0) < min_points:
            continue

        posted = item.get("time")
        if posted is None:
            continue
        if datetime.fromtimestamp(posted, tz=timezone.utc) < cutoff:
            continue

        paper = _story_to_paper(item, collection_date)
        if paper is not None:
            stories.append(paper)

    logger.info(
        "Hacker News: collected %d stories (>=%d points, last %d days)",
        len(stories),
        min_points,
        days_back,
    )
    return stories
