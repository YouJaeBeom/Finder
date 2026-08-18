"""Reading Notion back: what has this database already been given?

This is the **truth layer** for cross-run deduplication, and it exists because
the file-based one failed in production. On 2026-08-15 a backfill's ``git push``
lost to a race and ``seen_ids.json`` was discarded, taking 84 minutes of
collection and a ranking bill with it — and leaving the local state claiming 21
papers against a database holding far more. With ten members writing every week
that race stops being an accident and becomes the normal case.

So the question "has this member already received this paper?" is answered by
asking their database, not by trusting a file. Losing local state now costs a
lookup, never a duplicate page.

Two identifiers are recoverable from a Notion row and they are enough:

* **URL** — DOIs live inside it (``https://doi.org/10.18653/…``), so matching
  the URL matches the DOI without storing one.
* **Normalized title** — the fallback for rows whose URL is empty, and the
  reason a paper indexed under two different links is still caught.

A member's database is small by design — 20 pages a week is 1,000 rows a year,
ten requests — so this is cheap enough to do at the start of every member's turn.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Set

from .models import Paper, normalize_title
from .notion_api import check, request

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100


@dataclass
class WrittenIndex:
    """The URLs and titles already present in one database."""

    urls: Set[str] = field(default_factory=set)
    titles: Set[str] = field(default_factory=set)
    row_count: int = 0

    def contains(self, paper: Paper) -> bool:
        """Whether this item has already been written to the database."""
        if paper.url and paper.url in self.urls:
            return True
        title = paper.identifiers.normalized_title or normalize_title(paper.title)
        return bool(title) and title in self.titles

    def add(self, paper: Paper) -> None:
        """Record an item written during this run, so the run cannot repeat it.

        Within one run a paper can reach the same database twice — the same
        story carried by two feeds, say — and the index is what stops the second
        write. Cheaper and more reliable than re-querying Notion.
        """
        if paper.url:
            self.urls.add(paper.url)
        if title := (paper.identifiers.normalized_title
                     or normalize_title(paper.title)):
            self.titles.add(title)
        self.row_count += 1


def _plain_title(prop: dict) -> str:
    """The text of a Notion title property, however it was chunked.

    Notion splits rich text at formatting boundaries, so a title is a list of
    fragments rather than one string — reading only the first would truncate any
    title containing a link or an italicised word.
    """
    return "".join(part.get("plain_text", "") for part in prop.get("title", []))


def written_index(db_id: str, token: str) -> WrittenIndex:
    """Every URL and title already in *db_id*.

    Raises on failure rather than returning an empty index. An empty index reads
    as "nothing written yet", which would hand the member a duplicate of every
    paper they already have — a far worse outcome than skipping them for one run.
    """
    index = WrittenIndex()
    cursor: Optional[str] = None

    while True:
        payload: dict = {"page_size": _PAGE_SIZE}
        if cursor:
            payload["start_cursor"] = cursor

        resp = request("post", f"/databases/{db_id}/query", token,
                       what="database query", json_body=payload)
        check(resp, "database query")
        data = resp.json()

        for row in data.get("results", []):
            props = row.get("properties") or {}
            if url := (props.get("URL") or {}).get("url"):
                index.urls.add(url)
            if title := normalize_title(_plain_title(props.get("Title") or {})):
                index.titles.add(title)
            index.row_count += 1

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    logger.info("Notion database %s already holds %d row(s)", db_id, index.row_count)
    return index
