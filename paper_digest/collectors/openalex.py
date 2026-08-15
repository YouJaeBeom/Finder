"""OpenAlex paper collector using the public API (no auth required).

Uses ``from_publication_date`` (not ``from_created_date``) per the constraint that
'new' means published this week, not ingested this week.

Abstracts are served as ``abstract_inverted_index`` (token → position list) and
must be reconstructed to plain text before ranking.  Papers with no recoverable
abstract are excluded.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

import requests

from ..models import Paper, PaperIdentifiers, normalize_title
from ..venues import normalize_venue

logger = logging.getLogger(__name__)

OPENALEX_URL = "https://api.openalex.org/works"

# OpenAlex's polite pool gives a far higher rate limit to callers who identify
# themselves. The address is sent both ways because OpenAlex documents the
# query parameter and reads the User-Agent.
_DEFAULT_MAILTO = "paper-digest@example.com"

# Search terms are OR-ed together rather than sent one request each: the filter
# accepts "a|b" and returns the union (measured: "political bias" 360 +
# "retrieval augmented generation" 3,384, together 3,743). That turns ~95
# requests into ~8, which is the difference between being throttled and not.
_TERMS_PER_REQUEST = 12

_REQUEST_INTERVAL = 2.0
_MAX_ATTEMPTS = 5
_BACKOFF_BASE = 10.0

# A 429 can mean two different things now that OpenAlex meters by credit. A
# short Retry-After is ordinary throttling and worth waiting out; the
# daily-budget one comes back with the seconds remaining until midnight UTC,
# and no amount of backoff will outlast it.
_RETRYABLE_WAIT_SECONDS = 120


class BudgetExhausted(RuntimeError):
    """The day's OpenAlex credits are gone. Retrying cannot help."""


def _user_agent(mailto: str) -> str:
    return f"paper-digest/1.0 (mailto:{mailto})"


def _retry_after(resp) -> Optional[float]:
    for source in (resp.headers.get("Retry-After"),
                   resp.headers.get("x-ratelimit-reset")):
        try:
            return float(source)
        except (TypeError, ValueError):
            continue
    return None


def _get(params: dict, mailto: str, api_key: str = "") -> Optional[dict]:
    """One OpenAlex request, retrying while the API says to slow down.

    Raises BudgetExhausted when the day's credits are spent, so the caller
    stops instead of grinding through every remaining batch to be told the
    same thing.
    """
    params = {**params, "mailto": mailto}
    headers = {"User-Agent": _user_agent(mailto)}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.get(OPENALEX_URL, params=params, timeout=30,
                                headers=headers)

            if resp.status_code == 429:
                wait = _retry_after(resp)
                if wait is not None and wait > _RETRYABLE_WAIT_SECONDS:
                    raise BudgetExhausted(
                        f"OpenAlex daily credits are spent; they reset in "
                        f"{wait / 3600:.1f}h. Anonymous callers get 1,000 "
                        f"requests a day — set OPENALEX_API_KEY (a free "
                        f"account is 10x that) or run again tomorrow."
                    )
                wait = wait or _BACKOFF_BASE * (attempt + 1)
                logger.info("OpenAlex throttled — waiting %.0fs (attempt %d/%d)",
                            wait, attempt + 1, _MAX_ATTEMPTS)
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                wait = _BACKOFF_BASE * (attempt + 1)
                logger.info("OpenAlex returned %d — waiting %.0fs (attempt %d/%d)",
                            resp.status_code, wait, attempt + 1, _MAX_ATTEMPTS)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        except BudgetExhausted:
            raise
        except Exception as exc:
            wait = _BACKOFF_BASE * (attempt + 1)
            logger.info("OpenAlex error (%s) — retrying in %.0fs", exc, wait)
            time.sleep(wait)

    logger.warning("OpenAlex gave up after %d attempts", _MAX_ATTEMPTS)
    return None

# OpenAlex concept IDs for broad CS/AI/ML/NLP coverage
_CS_CONCEPTS = [
    "C41008148",   # Computer Science
    "C154945302",  # Artificial Intelligence
    "C119857082",  # Machine Learning
    "C204321447",  # Natural Language Processing
]

_SELECTED_FIELDS = ",".join([
    "id",
    "doi",
    "title",
    "abstract_inverted_index",
    "publication_date",
    "primary_location",
    "locations",  # the journal/conference is often not the primary location
    "authorships",
])


def reconstruct_abstract(inverted_index: Optional[Dict]) -> Optional[str]:
    """Reconstruct plain-text abstract from OpenAlex inverted index format.

    Returns None if the index is absent or empty (paper excluded from ranking).
    """
    if not inverted_index:
        return None

    position_to_token: Dict[int, str] = {}
    for token, positions in inverted_index.items():
        for pos in positions:
            position_to_token[pos] = token

    if not position_to_token:
        return None

    max_pos = max(position_to_token.keys())
    tokens = [position_to_token.get(i, "") for i in range(max_pos + 1)]
    text = " ".join(t for t in tokens if t).strip()
    return text if text else None


# OpenAlex source types that name where a paper was *published*. Everything
# else — chiefly "repository", which covers arXiv, Zenodo, OSF and institutional
# archives — is where a copy is *hosted*, which is not a venue.
_PUBLISHED_SOURCE_TYPES = {"journal", "conference", "book series", "ebook platform"}


def _locations(work: dict) -> List[dict]:
    """Every location for a work, primary first, without duplicates."""
    primary = work.get("primary_location") or {}
    seen = [primary] if primary else []
    for loc in work.get("locations") or []:
        if loc and loc not in seen:
            seen.append(loc)
    return seen


def _venue_name(location: dict, aliases: Dict[str, str]) -> str:
    """The short venue name for a location's source, e.g. CIKM rather than
    "Proceedings of the 32nd ACM International Conference on Information and
    Knowledge Management"."""
    source = location.get("source") or {}
    return normalize_venue(
        display_name=source.get("display_name") or "",
        abbreviated_title=source.get("abbreviated_title"),
        alternate_titles=source.get("alternate_titles"),
        aliases=aliases,
    )


def _pick_venue(work: dict, aliases: Optional[Dict[str, str]] = None) -> tuple[str, str]:
    """Return (venue, venue_status) — the journal or conference where possible.

    ``primary_location`` is whichever copy OpenAlex considers canonical, which
    for anything with a preprint is usually the repository. Taking it verbatim
    is why venues read "Zenodo" or "OpenAlex" instead of the conference the
    paper was actually presented at, so the published locations are searched
    first and repositories are only a fallback.

    Venue holds the name alone — "ACL", "arXiv". Whether that is a preprint or
    an accepted paper is the Status column's job, not a suffix on the name.
    """
    aliases = aliases or {}
    locations = _locations(work)

    for location in locations:
        source = location.get("source") or {}
        name = _venue_name(location, aliases)
        if name and (source.get("type") or "").lower() in _PUBLISHED_SOURCE_TYPES:
            return name, "published"

    for location in locations:
        if name := _venue_name(location, aliases):
            return name, "preprint"

    return "unknown", "preprint"


# General-purpose deposit archives. Anyone can upload anything to these with no
# moderation and get a DOI for it, so OpenAlex indexes manifestos and field
# guides alongside research. arXiv is also a repository but its CS sections are
# moderated, which is the difference that matters here.
DEFAULT_EXCLUDED_VENUES = (
    "zenodo",
    "figshare",
    "research square",
    "preprints.org",
    "authorea",
    "researchgate",
    "techrxiv",
)


def _is_excluded(venue: str, excluded: Sequence[str]) -> bool:
    lowered = venue.lower()
    return any(pattern.lower() in lowered for pattern in excluded)


def _parse_work(
    work: dict,
    aliases: Optional[Dict[str, str]] = None,
    excluded_venues: Optional[Sequence[str]] = None,
) -> Optional[Paper]:
    """Parse one OpenAlex work dict into a Paper."""
    title = (work.get("title") or "").strip()
    if not title:
        return None

    # Reconstruct abstract
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    # Papers with no abstract are excluded (per constraint)
    if not abstract:
        return None

    # DOI (normalise to bare DOI without https://doi.org/ prefix)
    raw_doi = work.get("doi") or ""
    doi: Optional[str] = None
    if raw_doi:
        doi = raw_doi.replace("https://doi.org/", "").strip()

    primary = work.get("primary_location") or {}
    venue_name, venue_status = _pick_venue(work, aliases)

    # Dropped only when the venue we settled on is an unmoderated archive —
    # _pick_venue prefers a real journal or conference, so a paper that is both
    # on Zenodo and in a proceedings keeps the proceedings and survives.
    excluded = DEFAULT_EXCLUDED_VENUES if excluded_venues is None else excluded_venues
    if _is_excluded(venue_name, excluded):
        logger.debug("Skipping %r from excluded venue %s", title[:60], venue_name)
        return None

    # Authors
    authors = [
        (a.get("author") or {}).get("display_name") or ""
        for a in (work.get("authorships") or [])
    ]
    authors = [a for a in authors if a]

    norm_title = normalize_title(title)

    identifiers = PaperIdentifiers(
        arxiv_id=None,
        doi=doi,
        normalized_title=norm_title,
    )

    # A DOI link is the stable, canonical address; the publisher's landing page
    # is the fallback for the works OpenAlex has without one.
    url = f"https://doi.org/{doi}" if doi else primary.get("landing_page_url")

    paper = Paper(
        identifiers=identifiers,
        title=title,
        abstract=abstract,
        authors=authors,
        venue=venue_name,
        venue_status=venue_status,
        collection_date=datetime.now(timezone.utc).date().isoformat(),
        source=["openalex"],
        url=url,
        published_at=work.get("publication_date"),
    )

    return paper


def search_terms_from(keywords: Sequence) -> List[str]:
    """The keyword terms selective enough to hand to OpenAlex's own search.

    Fetching by concept and date and filtering locally means downloading
    930,000 works for a year. Letting OpenAlex do the first pass cuts that to
    around 9,500 — but only if the terms are selective. Single words like
    "bias" or "dataset" match most of the corpus, so only multi-word phrases
    are sent; the full rule set still runs locally afterwards, so a rule that
    needs "bias" AND "steering" still applies to whatever comes back.
    """
    from ..keywords import compile_rules

    terms = {
        term.label
        for rule in compile_rules(keywords)
        for group in rule.groups
        for term in group
        if " " in term.label.strip()
    }
    return sorted(terms)


def collect_openalex_papers(
    keywords: List[str],
    days_back: int = 7,
    max_results: int = 500,
    venue_aliases: Optional[Dict[str, str]] = None,
    excluded_venues: Optional[Sequence[str]] = None,
    by_index_date: bool = False,
    mailto: str = _DEFAULT_MAILTO,
    api_key: str = "",
) -> List[Paper]:
    """Fetch papers from OpenAlex within the last *days_back* days.

    *by_index_date* switches the window from "published since" to "indexed
    since" — see the comment on date_field below for why a weekly run wants
    the latter and a one-off backfill wants the former.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).date()
    date_str = cutoff.isoformat()

    concept_filter = "|".join(_CS_CONCEPTS)
    # is_core restricts to venues OpenAlex classifies as core scholarly sources.
    # It is the difference between 79,000 and 9,500 works a week, and it removes
    # the unmoderated deposit archives at the source rather than by name — 36%
    # of recent CS "works" were Zenodo uploads before this.
    #
    # from_created_date asks "indexed since", from_publication_date asks
    # "published since". A weekly digest wants the former: a paper published in
    # May but indexed today is new *to us*, and a publication-date window would
    # miss it permanently.
    date_field = "from_created_date" if by_index_date else "from_publication_date"
    base_filter = (
        f"{date_field}:{date_str},concepts.id:{concept_filter},"
        f"primary_location.source.is_core:true"
    )

    terms = search_terms_from(keywords)
    batches = (
        [terms[i : i + _TERMS_PER_REQUEST]
         for i in range(0, len(terms), _TERMS_PER_REQUEST)]
        or [[]]
    )
    logger.info("OpenAlex: %d search terms in %d requests over %s since %s",
                len(terms), len(batches), date_field, date_str)

    papers: List[Paper] = []
    seen_ids: set = set()

    for batch in batches:
        if len(papers) >= max_results:
            break
        work_filter = base_filter
        if batch:
            work_filter += f",title_and_abstract.search:{'|'.join(batch)}"
        try:
            _collect_one_search(work_filter, max_results, papers, seen_ids,
                                venue_aliases, excluded_venues, mailto, api_key)
        except BudgetExhausted as exc:
            # Keep what was collected; the rest of the batches would only hit
            # the same wall.
            logger.error("%s", exc)
            break

    logger.info("OpenAlex: collected %d papers (last %d days)", len(papers), days_back)
    return papers[:max_results]


def _collect_one_search(
    work_filter: str,
    max_results: int,
    papers: List[Paper],
    seen_ids: set,
    venue_aliases: Optional[Dict[str, str]],
    excluded_venues: Optional[Sequence[str]],
    mailto: str,
    api_key: str,
) -> None:
    """Page through one search and append its new papers to *papers*."""
    cursor = "*"

    while len(papers) < max_results:
        params = {
            "filter": work_filter,
            "select": _SELECTED_FIELDS,
            "per_page": 200,
            "cursor": cursor,
        }

        time.sleep(_REQUEST_INTERVAL)
        data = _get(params, mailto, api_key)
        if data is None:
            break

        results = data.get("results") or []
        meta = data.get("meta") or {}

        for work in results:
            # Searches overlap heavily — one paper matches several phrases.
            if work.get("id") in seen_ids:
                continue
            seen_ids.add(work.get("id"))
            paper = _parse_work(work, venue_aliases, excluded_venues)
            if paper is not None:
                papers.append(paper)

        next_cursor = meta.get("next_cursor")
        if not next_cursor or not results:
            break
        cursor = next_cursor
