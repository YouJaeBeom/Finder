"""arXiv paper collector using the public Atom/XML API.

Only papers where v1 was submitted within the last N days are included.
Revisions (v2+) are excluded per the constraint that 'new' means first submission.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

from ..models import Paper, PaperIdentifiers, normalize_title

logger = logging.getLogger(__name__)

ARXIV_API_URL = "http://export.arxiv.org/api/query"
ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"

# arXiv polite rate limit: 3 seconds between requests
_REQUEST_INTERVAL = 3.0


def _parse_arxiv_id(raw_id: str) -> Optional[str]:
    """Extract bare arXiv ID without version suffix from a full URL or ID string.

    Examples:
        'http://arxiv.org/abs/2401.12345v1' -> '2401.12345'
        '2401.12345v2' -> None  (v2, excluded)
        '2401.12345v1' -> '2401.12345'
    """
    # Extract the ID part (last segment of URL or plain ID)
    match = re.search(r"(\d{4}\.\d{4,5})(v\d+)?$", raw_id)
    if not match:
        return None
    arxiv_num = match.group(1)
    version = match.group(2) or "v1"
    if version != "v1":
        return None  # Exclude revisions
    return arxiv_num


def _parse_entry(
    entry: ET.Element,
    keywords: List[str],
    cutoff: datetime,
) -> Optional[Paper]:
    """Parse one Atom entry element into a Paper, or return None if filtered out."""

    def tag(ns: str, name: str) -> str:
        return f"{{{ns}}}{name}"

    # Published date (v1 submission date)
    published_el = entry.find(tag(ATOM_NS, "published"))
    if published_el is None or not published_el.text:
        return None
    try:
        published_dt = datetime.fromisoformat(
            published_el.text.replace("Z", "+00:00")
        )
    except ValueError:
        return None

    if published_dt < cutoff:
        return None  # Too old

    # arXiv ID (must be v1)
    id_el = entry.find(tag(ATOM_NS, "id"))
    if id_el is None or not id_el.text:
        return None
    arxiv_id = _parse_arxiv_id(id_el.text)
    if arxiv_id is None:
        return None  # v2+ revision; skip

    # Title
    title_el = entry.find(tag(ATOM_NS, "title"))
    title = (title_el.text or "").strip().replace("\n", " ") if title_el is not None else ""
    if not title:
        return None

    # Abstract
    summary_el = entry.find(tag(ATOM_NS, "summary"))
    abstract: Optional[str] = None
    if summary_el is not None and summary_el.text:
        abstract = summary_el.text.strip().replace("\n", " ")

    # Authors
    authors = [
        (name_el.text or "").strip()
        for author_el in entry.findall(tag(ATOM_NS, "author"))
        if (name_el := author_el.find(tag(ATOM_NS, "name"))) is not None
    ]

    norm_title = normalize_title(title)

    identifiers = PaperIdentifiers(
        arxiv_id=arxiv_id,
        doi=None,  # arXiv API doesn't provide DOI; link to DOI via arxiv_id later
        normalized_title=norm_title,
    )

    paper = Paper(
        identifiers=identifiers,
        title=title,
        abstract=abstract,
        authors=authors,
        # The venue is the name alone; that it is a preprint is what
        # venue_status says, and what the Notion Status column shows.
        venue="arXiv",
        venue_status="preprint",
        collection_date=datetime.now(timezone.utc).date().isoformat(),
        source=["arxiv"],
        # The abstract page rather than the PDF: it carries the metadata, the
        # PDF link, and the version history.
        url=f"https://arxiv.org/abs/{arxiv_id}",
        # v1 submission date — the paper's own date, not the day we fetched it.
        published_at=published_dt.date().isoformat(),
    )

    return paper


def collect_arxiv_papers(
    categories: List[str],
    keywords: List[str],
    days_back: int = 7,
    max_results: int = 500,
) -> List[Paper]:
    """Fetch papers from arXiv published (v1) in the last *days_back* days.

    Returns at most *max_results* papers matching the category filter.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    cat_query = " OR ".join(f"cat:{c}" for c in categories)
    # Date range (coarse — we validate precisely via published date)
    start_str = cutoff.strftime("%Y%m%d") + "0000"
    end_str = datetime.now(timezone.utc).strftime("%Y%m%d") + "2359"
    date_filter = f"submittedDate:[{start_str}+TO+{end_str}]"
    query = f"({cat_query}) AND {date_filter}"

    papers: List[Paper] = []
    start = 0
    batch = 200
    last_request_time = 0.0

    while len(papers) < max_results:
        # Respect rate limit
        elapsed = time.time() - last_request_time
        if elapsed < _REQUEST_INTERVAL:
            time.sleep(_REQUEST_INTERVAL - elapsed)

        params = {
            "search_query": query,
            "start": start,
            "max_results": min(batch, max_results - len(papers)),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }

        try:
            resp = requests.get(ARXIV_API_URL, params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("arXiv API error at start=%d: %s", start, exc)
            break
        finally:
            last_request_time = time.time()

        root = ET.fromstring(resp.content)
        ns = {"atom": ATOM_NS}
        entries = root.findall("atom:entry", ns)

        if not entries:
            break

        added_this_batch = 0
        for entry in entries:
            paper = _parse_entry(entry, keywords, cutoff)
            if paper is not None:
                papers.append(paper)
                added_this_batch += 1

        if added_this_batch == 0:
            # Entries exist but none passed the cutoff — we're into older papers
            break

        start += len(entries)
        if len(entries) < batch:
            break  # Last page

    logger.info("arXiv: collected %d papers (v1, last %d days)", len(papers), days_back)
    return papers
