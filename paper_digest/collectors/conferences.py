"""Top-conference collector, via the Semantic Scholar Graph API.

OpenAlex cannot do this. Its conference records are unusable: ACL papers carry
``source: null``, the conference sources that exist are per-year fragments
("Proceedings of the 60th Annual Meeting…"), and across all of computer science
only a few hundred works a month have a conference source at all. Filtering
OpenAlex by venue would silently drop exactly the venues worth reading.

Semantic Scholar indexes the same papers *with* their venue and their abstract,
accepts the conference abbreviation directly, and takes a publication-date
range — everything this needs in one request. No API key required.

Conference proceedings land in bursts rather than a steady trickle, so a given
week is usually quiet and then suddenly carries a whole conference. That is the
intended behaviour, not a bug.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Mapping, Optional, Sequence

import requests

from ..models import Paper, PaperIdentifiers, normalize_title
from ..venues import normalize_venue, venue_aliases_from_list

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
TIMEOUT = 60

_FIELDS = "title,abstract,venue,publicationDate,year,externalIds,authors"

# Venues per request. The filter goes in the query string, so the whole list
# has to fit in a URL; batching also keeps one unrecognised name from costing
# the rest.
_VENUES_PER_REQUEST = 25

# Unauthenticated Semantic Scholar runs on a shared pool, so the safe rate is
# well under one request a second — a 1.1s gap lost three of four batches to
# 429s during a year-long backfill, taking ACL, NeurIPS, CHI, SIGIR and CVPR
# with them. Slower here costs seconds; being throttled costs whole venues.
_REQUEST_INTERVAL = 3.0

# A 429 means "later", not "never", so it is retried rather than dropped. Each
# wait is longer than the last.
_MAX_ATTEMPTS = 5
_BACKOFF_BASE = 5.0


def _batches(items: Sequence[str], size: int) -> List[Sequence[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _paper_url(external: Dict[str, str], paper_id: str) -> Optional[str]:
    """The most durable link available for a paper."""
    if doi := external.get("DOI"):
        return f"https://doi.org/{doi}"
    if arxiv := external.get("ArXiv"):
        return f"https://arxiv.org/abs/{arxiv}"
    return f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else None


def _to_paper(item: dict, venue_label: str, collection_date: str) -> Optional[Paper]:
    """Convert one Semantic Scholar record into a Paper.

    Papers with no abstract are dropped, matching the rule the rest of the
    pipeline follows: a title alone is not enough to rank research relevance.
    """
    title = (item.get("title") or "").strip()
    abstract = (item.get("abstract") or "").strip()
    if not title or not abstract:
        return None

    external = item.get("externalIds") or {}
    doi = external.get("DOI")
    arxiv_id = external.get("ArXiv")

    authors = [a.get("name", "") for a in (item.get("authors") or [])]

    return Paper(
        identifiers=PaperIdentifiers(
            arxiv_id=arxiv_id,
            doi=doi,
            normalized_title=normalize_title(title),
        ),
        title=title,
        abstract=abstract,
        authors=[a for a in authors if a],
        venue=venue_label,
        # These are proceedings papers — accepted and published, not preprints.
        venue_status="published",
        collection_date=collection_date,
        source=["conference"],
        url=_paper_url(external, item.get("paperId") or ""),
        published_at=item.get("publicationDate"),
    )


def _search(params: dict, label: str) -> Optional[dict]:
    """One search request, retrying while the API says to slow down.

    Returns None when the batch could not be fetched — one bad batch must not
    take down the rest of the collection.
    """
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.get(SEARCH_URL, params=params, timeout=TIMEOUT)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = _BACKOFF_BASE * (attempt + 1)
                logger.info("Semantic Scholar returned %d for %s… — waiting %.0fs "
                            "(attempt %d/%d)", resp.status_code, label, wait,
                            attempt + 1, _MAX_ATTEMPTS)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            # Broad on purpose: a truncated body raises from resp.json(), not
            # from requests, and that is as transient as a dropped connection.
            wait = _BACKOFF_BASE * (attempt + 1)
            logger.info("Conference search error for %s… (%s) — retrying in %.0fs",
                        label, exc, wait)
            time.sleep(wait)

    logger.warning("Conference search gave up on %s… after %d attempts",
                   label, _MAX_ATTEMPTS)
    return None


def _label_for(returned_venue: str, requested: Mapping[str, str]) -> Optional[str]:
    """The abbreviation for a returned venue, or None if we never asked for it.

    Two things happen here. A batch asks for many venues at once and the reply
    names each paper's venue in full, so the abbreviation has to be recovered —
    "Annual Meeting of the Association for Computational Linguistics" does not
    even contain the letters "acl", so this cannot be a substring test alone.

    And Semantic Scholar's venue filter matches loosely: a request for a batch
    of conferences came back carrying papers from a journal called "Languages".
    Returning None for anything we did not ask for drops those rather than
    filing them under a venue nobody chose.
    """
    if not returned_venue:
        return None

    wanted = set(requested.values())

    # The shipped table maps full registered names onto their abbreviations.
    canonical = normalize_venue(returned_venue, aliases=venue_aliases_from_list())
    if canonical in wanted:
        return canonical

    lowered = returned_venue.lower()
    for query, abbr in requested.items():
        if query.lower() in lowered or abbr.lower() in lowered:
            return abbr

    logger.debug("Dropping paper from unrequested venue %r", returned_venue)
    return None


def collect_conference_papers(
    venues: Mapping[str, str],
    days_back: int = 7,
    max_results: int = 500,
) -> List[Paper]:
    """Fetch papers published at *venues* within the last *days_back* days.

    *venues* maps the name Semantic Scholar answers to onto the abbreviation to
    file the paper under.
    """
    if not venues:
        return []

    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=days_back)
    date_range = f"{since.isoformat()}:{today.isoformat()}"
    collection_date = today.isoformat()

    papers: List[Paper] = []
    last_request = 0.0

    for batch_keys in _batches(list(venues), _VENUES_PER_REQUEST):
        batch = {k: venues[k] for k in batch_keys}
        if len(papers) >= max_results:
            break

        elapsed = time.time() - last_request
        if elapsed < _REQUEST_INTERVAL:
            time.sleep(_REQUEST_INTERVAL - elapsed)

        params = {
            "query": "",
            "venue": ",".join(batch),
            "publicationDateOrYear": date_range,
            "fields": _FIELDS,
        }

        # One response carries at most 1,000 papers and a continuation token.
        # A year of the twelve best-known venues alone is 13,544, so ignoring
        # the token silently truncates a backfill to the first page.
        token: Optional[str] = None
        while len(papers) < max_results:
            if token:
                params = {**params, "token": token}
                elapsed = time.time() - last_request
                if elapsed < _REQUEST_INTERVAL:
                    time.sleep(_REQUEST_INTERVAL - elapsed)

            data = _search(params, batch_keys[0])
            last_request = time.time()
            if data is None:
                break

            for item in data.get("data") or []:
                label = _label_for(item.get("venue", ""), batch)
                if label is None:
                    continue
                paper = _to_paper(item, label, collection_date)
                if paper is not None:
                    papers.append(paper)

            token = data.get("token")
            if not token:
                break

    logger.info(
        "Conferences: collected %d papers from %d venues (last %d days)",
        len(papers), len(venues), days_back,
    )
    return papers[:max_results]
