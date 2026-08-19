"""RSS / Atom feed collector.

Feed URLs come from the config file, so adding a publication is an edit to
config.yaml rather than a code change — the same rule the rest of the tool
follows.

Uses feedparser rather than hand-rolled XML parsing: real-world feeds mix RSS
2.0, Atom and RDF with inconsistent date formats, and normalising that by hand
is a reliable source of silent misses.

The bytes are fetched with ``requests`` and handed to feedparser, rather than
letting it fetch for itself. Two reasons, both found the hard way: feedparser
downloads through urllib, which uses the interpreter's own CA store and fails
outright on a machine where that was never populated — every feed came back
"no usable entries" while curl fetched all three fine. And feedparser announces
itself with its own User-Agent, which some publishers refuse.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlparse

import feedparser
import requests

from ..models import Paper, PaperIdentifiers, normalize_title

logger = logging.getLogger(__name__)

TIMEOUT = 20
# Plain feedparser identification is refused by some publishers; a browser-ish
# string is what their feeds are actually served to.
_USER_AGENT = (
    "Mozilla/5.0 (compatible; paper-digest/1.0; "
    "+https://github.com/YouJaeBeom/Finder)"
)

_TAG_RE = re.compile(r"<[^>]+>")
_SUMMARY_MAX_CHARS = 1500


def _strip_html(text: str) -> str:
    """Reduce a feed's HTML summary to plain text for the ranking prompt."""
    if not text:
        return ""
    plain = _TAG_RE.sub(" ", text)
    plain = html.unescape(plain)
    return re.sub(r"\s+", " ", plain).strip()


def _entry_datetime(entry) -> Optional[datetime]:
    """Best-effort published/updated timestamp as an aware UTC datetime."""
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _feed_name(parsed, feed_url: str) -> str:
    """Human-readable source label, falling back to the feed's hostname."""
    title = getattr(parsed.feed, "title", "") if hasattr(parsed, "feed") else ""
    if title:
        return title.strip()[:100]
    return urlparse(feed_url).netloc or "RSS"


def collect_rss_entries(feed_urls: List[str], days_back: int = 7) -> List[Paper]:
    """Fetch entries published within *days_back* days from each feed URL."""
    if not feed_urls:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    collection_date = datetime.now(timezone.utc).date().isoformat()
    items: List[Paper] = []

    for feed_url in feed_urls:
        try:
            resp = requests.get(feed_url, timeout=TIMEOUT,
                                headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            # One bad feed must not take down the whole run.
            logger.warning("RSS: failed to fetch %s: %s", feed_url, exc)
            continue

        if getattr(parsed, "bozo", False) and not getattr(parsed, "entries", []):
            logger.warning("RSS: no usable entries from %s", feed_url)
            continue

        source_name = _feed_name(parsed, feed_url)
        kept = 0

        for entry in getattr(parsed, "entries", []):
            title = (getattr(entry, "title", "") or "").strip()
            link = getattr(entry, "link", "") or ""
            if not title or not link:
                continue

            published = _entry_datetime(entry)
            # Feeds that omit dates entirely are kept: dropping them would
            # silently lose whole publications. Dedup stops repeat delivery.
            if published is not None and published < cutoff:
                continue

            summary = _strip_html(
                getattr(entry, "summary", "") or getattr(entry, "description", "")
            )[:_SUMMARY_MAX_CHARS]

            author = getattr(entry, "author", "") or ""

            items.append(Paper(
                identifiers=PaperIdentifiers(
                    arxiv_id=None,
                    doi=None,
                    normalized_title=normalize_title(title),
                    url=link,
                ),
                title=title,
                abstract=summary or None,
                authors=[author] if author else [],
                venue=source_name,
                venue_status="published",
                collection_date=collection_date,
                source=["rss"],
                content_type="news",
                url=link,
                # No community score in a feed — recency is all the ordering
                # signal RSS gives us.
                published_at=published.isoformat() if published else None,
            ))
            kept += 1

        logger.info("RSS: %d entries from %s", kept, source_name)

    logger.info("RSS: collected %d entries from %d feeds", len(items), len(feed_urls))
    return items
