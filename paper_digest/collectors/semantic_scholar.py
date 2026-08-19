"""The paper collector: top conferences and journals, via Semantic Scholar.

This is the only paper source. Everything it returns comes from a venue on the
shipped allowlist (``paper_digest/data/venues.csv``), which is the entire
quality mechanism — a digest is only as good as the venues it reads.

Two earlier sources were removed rather than kept:

* **OpenAlex.** Its conference records were unusable to begin with (ACL papers
  carry ``source: null``, and the conference sources that exist are per-year
  fragments). Then in 2026 it moved ``from_created_date`` and
  ``from_updated_date`` behind a paid plan, which is what a monthly digest needs
  — "indexed since last week", not "published since last week". What remains
  free is a publication-date window over a topic filter so loose that the top
  venue by volume was a predatory electromagnetics journal publishing NLP
  papers. Restricted to an allowlist it would return what this module already
  returns, so it earned nothing.

* **arXiv.** Hundreds of preprints a day against a conference's once-a-year
  proceedings, so with both enabled essentially every slot went to a preprint.
  It had been disabled by config for months before the code was removed.

Semantic Scholar takes the venue name and a publication-date range and answers
with the venue, the abstract and the identifiers in one request. No API key.

Proceedings land in bursts rather than a steady trickle, so a given week is
usually quiet on the conference side and then suddenly carries a whole
conference. Journals fill the quiet weeks. That is the intended behaviour.
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


def _batch_label(batch_keys: Sequence[str]) -> str:
    """A short, honest name for a batch of venues in a log line.

    "SIGGRAPH…" was the old form and it hid the other 24 venues in the request,
    which is exactly the information someone reading a thin week needs.
    """
    if len(batch_keys) <= 3:
        return ", ".join(batch_keys)
    return f"{batch_keys[0]} … {batch_keys[-1]} ({len(batch_keys)} venues)"


def _paper_url(external: Dict[str, str], paper_id: str) -> Optional[str]:
    """The most durable link available for a paper."""
    if doi := external.get("DOI"):
        return f"https://doi.org/{doi}"
    if arxiv := external.get("ArXiv"):
        return f"https://arxiv.org/abs/{arxiv}"
    return f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else None


def _to_paper(
    item: dict,
    venue_label: str,
    collection_date: str,
    source_label: str,
) -> Optional[Paper]:
    """Convert one Semantic Scholar record into a Paper.

    Papers with no abstract are dropped, matching the rule the rest of the
    pipeline follows: a title alone is not enough to rank research relevance.
    Coverage is uneven — some publishers (Elsevier especially) hand Semantic
    Scholar metadata without abstracts, so a journal can contribute far fewer
    papers than its volume suggests.
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
        # Everything on the allowlist is peer-reviewed and out — proceedings
        # papers and journal articles alike, never preprints.
        venue_status="published",
        collection_date=collection_date,
        source=[source_label],
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
                logger.info("Semantic Scholar returned %d for [%s] — waiting "
                            "%.0fs (attempt %d/%d)", resp.status_code, label,
                            wait, attempt + 1, _MAX_ATTEMPTS)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            # Broad on purpose: a truncated body raises from resp.json(), not
            # from requests, and that is as transient as a dropped connection.
            wait = _BACKOFF_BASE * (attempt + 1)
            logger.info("Search error for [%s] (%s) — retrying in %.0fs",
                        label, exc, wait)
            time.sleep(wait)

    logger.warning("Gave up on [%s] after %d attempts", label, _MAX_ATTEMPTS)
    return None


def _label_for(
    returned_venue: str,
    requested: Mapping[str, str],
    exact: bool = False,
) -> Optional[str]:
    """The abbreviation for a returned venue, or None if we never asked for it.

    Two things happen here. A batch asks for many venues at once and the reply
    names each paper's venue in full, so the abbreviation has to be recovered —
    "Annual Meeting of the Association for Computational Linguistics" does not
    even contain the letters "acl", so this cannot be a substring test alone.

    And Semantic Scholar's venue filter matches loosely: a request for a batch
    of conferences came back carrying papers from a journal called "Languages".
    Returning None for anything we did not ask for drops those rather than
    filing them under a venue nobody chose.

    *exact* is for journals, where the loose fallback is actively wrong in both
    directions. Asking for "Big Data & Society" returns a different journal
    called "Big Data"; asking for "Artificial Intelligence" would file every
    paper from "Artificial Intelligence Review" under it. Journals answer with
    their exact registered name, so requiring one costs nothing and removes a
    whole class of silent mislabelling. Conferences still need the loose path —
    their returned names carry a year, an ordinal and a host city.
    """
    if not returned_venue:
        return None

    lowered = returned_venue.lower()

    if exact:
        for query, abbr in requested.items():
            if query.lower() == lowered:
                return abbr
        logger.debug("Dropping paper from near-miss venue %r", returned_venue)
        return None

    wanted = set(requested.values())

    # The shipped table maps full registered names onto their abbreviations.
    canonical = normalize_venue(returned_venue, aliases=venue_aliases_from_list())
    if canonical in wanted:
        return canonical

    for query, abbr in requested.items():
        if query.lower() in lowered or abbr.lower() in lowered:
            return abbr

    logger.debug("Dropping paper from unrequested venue %r", returned_venue)
    return None


def collect_venue_papers(
    venues: Mapping[str, str],
    days_back: int = 7,
    max_results: int = 500,
    source_label: str = "conference",
    exact_venue_match: bool = False,
) -> List[Paper]:
    """Fetch papers published at *venues* within the last *days_back* days.

    *venues* maps the name Semantic Scholar answers to onto the abbreviation to
    file the paper under. *source_label* lands in ``Paper.source``, and
    *exact_venue_match* tightens venue matching for journals — see
    :func:`_label_for`.
    """
    if not venues:
        return []

    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=days_back)
    date_range = f"{since.isoformat()}:{today.isoformat()}"
    collection_date = today.isoformat()

    papers: List[Paper] = []
    last_request = 0.0
    # Venues this run could not reach. Reported by name at the end: the venue
    # allowlist *is* the coverage, so a batch quietly dropping out means a whole
    # conference contributed nothing and nobody was told.
    unreachable: List[str] = []

    for batch_keys in _batches(list(venues), _VENUES_PER_REQUEST):
        batch = {k: venues[k] for k in batch_keys}
        label = _batch_label(batch_keys)
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
        pages = 0
        while len(papers) < max_results:
            if token:
                params = {**params, "token": token}
                elapsed = time.time() - last_request
                if elapsed < _REQUEST_INTERVAL:
                    time.sleep(_REQUEST_INTERVAL - elapsed)

            data = _search(params, label)
            last_request = time.time()
            if data is None:
                # Failing on the first page loses the batch outright; failing
                # later keeps what already arrived. Different losses, said
                # differently.
                if pages == 0:
                    unreachable.extend(batch.values())
                else:
                    logger.warning("[%s] stopped after %d page(s) — the rest of "
                                   "this batch is missing from this run",
                                   label, pages)
                break
            pages += 1

            for item in data.get("data") or []:
                label = _label_for(item.get("venue", ""), batch, exact_venue_match)
                if label is None:
                    continue
                paper = _to_paper(item, label, collection_date, source_label)
                if paper is not None:
                    papers.append(paper)

            token = data.get("token")
            if not token:
                break

    if unreachable:
        # Unauthenticated Semantic Scholar throttles a caller that collects
        # repeatedly in a short window, and it reports that as a 404 about as
        # often as a 429 — so this is usually transient. Named anyway: a week
        # that looks quiet because SIGIR never answered is not a quiet week.
        logger.warning(
            "%s: %d of %d venues could not be fetched this run and contributed "
            "nothing — %s",
            source_label.capitalize(), len(unreachable), len(venues),
            ", ".join(sorted(unreachable)),
        )

    logger.info(
        "%s: collected %d papers from %d of %d venues (last %d days)",
        source_label.capitalize(), len(papers),
        len(venues) - len(unreachable), len(venues), days_back,
    )
    return papers[:max_results]
