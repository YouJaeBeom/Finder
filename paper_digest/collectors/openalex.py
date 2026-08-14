"""OpenAlex paper collector using the public API (no auth required).

Uses ``from_publication_date`` (not ``from_created_date``) per the constraint that
'new' means published this week, not ingested this week.

Abstracts are served as ``abstract_inverted_index`` (token → position list) and
must be reconstructed to plain text before ranking.  Papers with no recoverable
abstract are excluded.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from ..models import Paper, PaperIdentifiers, normalize_title

logger = logging.getLogger(__name__)

OPENALEX_URL = "https://api.openalex.org/works"

# Polite pool: include an email in the User-Agent for higher rate limits
_USER_AGENT = "paper-digest/1.0 (mailto:research@example.com)"

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


def _source_name(location: dict) -> str:
    return ((location.get("source") or {}).get("display_name") or "").strip()


def _pick_venue(work: dict) -> tuple[str, str]:
    """Return (venue, venue_status) — the journal or conference where possible.

    ``primary_location`` is whichever copy OpenAlex considers canonical, which
    for anything with a preprint is usually the repository. Taking it verbatim
    is why venues read "Zenodo" or "OpenAlex" instead of the conference the
    paper was actually presented at, so the published locations are searched
    first and repositories are only a fallback.
    """
    locations = _locations(work)

    for location in locations:
        source = location.get("source") or {}
        name = _source_name(location)
        if name and (source.get("type") or "").lower() in _PUBLISHED_SOURCE_TYPES:
            return name[:100], "published"

    # Only repository copies exist, so this is a preprint. Say so in the venue
    # itself — batch mode looks for exactly that word when stamping an accepted
    # venue onto pages later.
    for location in locations:
        name = _source_name(location)
        if name:
            if "arxiv" in name.lower():
                return "arXiv preprint", "preprint"
            return f"{name} preprint"[:100], "preprint"

    return "preprint", "preprint"


def _parse_work(work: dict) -> Optional[Paper]:
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
    venue_name, venue_status = _pick_venue(work)

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


def collect_openalex_papers(
    keywords: List[str],
    days_back: int = 7,
    max_results: int = 500,
) -> List[Paper]:
    """Fetch papers from OpenAlex published in the last *days_back* days.

    Uses from_publication_date to target genuinely new papers rather than
    OpenAlex ingestion date (from_created_date).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).date()
    date_str = cutoff.isoformat()

    concept_filter = "|".join(_CS_CONCEPTS)
    work_filter = f"from_publication_date:{date_str},concepts.id:{concept_filter}"

    papers: List[Paper] = []
    cursor = "*"

    while len(papers) < max_results:
        params = {
            "filter": work_filter,
            "select": _SELECTED_FIELDS,
            "per_page": min(200, max_results - len(papers)),
            "cursor": cursor,
        }

        try:
            resp = requests.get(
                OPENALEX_URL,
                params=params,
                headers={"User-Agent": _USER_AGENT},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.error("OpenAlex API error: %s", exc)
            break

        results = data.get("results") or []
        meta = data.get("meta") or {}

        for work in results:
            paper = _parse_work(work)
            if paper is not None:
                papers.append(paper)

        # Pagination
        next_cursor = meta.get("next_cursor")
        if not next_cursor or not results:
            break
        cursor = next_cursor

    logger.info("OpenAlex: collected %d papers (last %d days)", len(papers), days_back)
    return papers
