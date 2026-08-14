"""Notion integration: resolve one durable database and write/update pages.

Every run must land in the *same* database — a digest that scatters itself
across a new database each week is worthless. ``ensure_database`` therefore
resolves an existing database before it will create one, in this order:

    1. ``notion_database_id`` in config.yaml — explicit, survives fresh checkouts
    2. ``state.json`` — the local cache written after a create
    3. a database already sitting under the parent page with our title
    4. create a new one (first run only)

Step 3 is what makes GitHub Actions safe: CI checks out a clean tree with no
state.json, and without the lookup every scheduled run would create a duplicate
database.

DB schema:
    Title     (title)          Type      (select: 논문 | 뉴스)
    Venue     (select)         Score     (number, papers only)
    Status    (select: preprint | published | accepted)
    Tags      (multi_select)   URL       (url)
    Published (date)  — the item's own date: arXiv v1 submission, OpenAlex
                        publication_date, or the news story's timestamp
    Collected (date)  — the day a run fetched it. Named "Date" before it sat
                        next to Published and had to say which one it was.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests

from .models import Paper, ResearchNote

logger = logging.getLogger(__name__)

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"

_STATE_FILE = "state.json"
_DB_TITLE = "📚 Paper Digest"


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def _check(resp, what: str) -> None:
    """Raise on an error response, quoting what Notion actually said.

    ``raise_for_status`` alone yields "401 Client Error: Unauthorized", which
    tells a user nothing. Notion's body carries the sentence that matters — for
    example "Make sure the relevant pages and databases are shared with your
    integration" — and that is the difference between a fixable error and a
    baffling one.
    """
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        try:
            detail = resp.json().get("message") or resp.text[:300]
        except Exception:
            detail = (resp.text or "")[:300]
        raise RuntimeError(f"Notion {what} failed: {detail}") from exc


# ── State (DB ID persistence) ──────────────────────────────────────────────────

def _load_state() -> dict:
    p = Path(_STATE_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict) -> None:
    Path(_STATE_FILE).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── Database creation ──────────────────────────────────────────────────────────

_DB_PROPERTIES: Dict[str, dict] = {
    "Title": {"title": {}},
    "Type": {"select": {}},       # "논문" | "뉴스" — papers and news share one DB
    "Venue": {"select": {}},      # paper: ACL 2026 / arXiv preprint; news: source site
    "Status": {"select": {}},     # preprint | published | accepted
    "Score": {"number": {"format": "number"}},
    "Tags": {"multi_select": {}},
    "Published": {"date": {}},    # the item's own date — what you sort a digest by
    "Collected": {"date": {}},    # the day a run happened to fetch it
    "URL": {"url": {}},
}

# Properties an earlier version of this tool created under a different name.
# Renaming keeps the column's existing values — every "Date" ever written was a
# collection date, so the rows stay correct under the clearer name.
_RENAMED_PROPERTIES = {"Date": "Collected"}


def _find_database_under_page(parent_page_id: str, token: str) -> Optional[str]:
    """Return the ID of a database already living under the parent page.

    Matched by title, so a database this tool created on an earlier run — or on
    another machine — is reused instead of duplicated.
    """
    cursor: Optional[str] = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        try:
            resp = requests.get(
                f"{NOTION_BASE_URL}/blocks/{parent_page_id}/children",
                headers=_headers(token),
                params=params,
                timeout=30,
            )
        except requests.RequestException as exc:
            # Falling through to create is worse than failing loudly here: a
            # transient error would silently produce a duplicate database.
            raise RuntimeError(
                f"Could not list children of parent page {parent_page_id}: {exc}"
            ) from exc
        _check(resp, f"page lookup ({parent_page_id})")

        data = resp.json()
        for block in data.get("results", []):
            if block.get("type") != "child_database":
                continue
            if block["child_database"].get("title") == _DB_TITLE:
                return block["id"]

        if not data.get("has_more"):
            return None
        cursor = data.get("next_cursor")


def read_properties(db_id: str, token: str) -> Set[str]:
    """The column names the database currently has."""
    resp = requests.get(
        f"{NOTION_BASE_URL}/databases/{db_id}", headers=_headers(token), timeout=30
    )
    _check(resp, "read database schema")
    return set(resp.json().get("properties", {}))


def _sync_schema(db_id: str, token: str) -> Set[str]:
    """Bring an existing database's columns up to the current schema.

    Returns the column names that exist afterwards — callers write only those,
    since Notion rejects a page carrying a property the database doesn't have.

    Databases created by earlier versions of this tool are missing columns that
    were added later. Renames come first, so a column that only needs renaming
    is not also added as a second, empty column beside it.

    A failure here is reported and swallowed rather than raised. Notion accepts
    some schema edits and refuses others, and losing an entire week's digest
    because one column could not be added is a wildly disproportionate outcome
    — better to write the columns that do exist and say what was skipped.
    """
    existing = read_properties(db_id, token)

    # `existing` stays the columns that are really there, so the failure path
    # can report them honestly. `planned` is what would exist if the patch
    # lands, and is what the add-missing pass is computed against — otherwise a
    # renamed column is also created a second time, empty, under its new name.
    planned = set(existing)

    patch: Dict[str, dict] = {}
    for old_name, new_name in _RENAMED_PROPERTIES.items():
        if old_name in existing and new_name not in existing:
            patch[old_name] = {"name": new_name}
            planned.discard(old_name)
            planned.add(new_name)

    patch.update({
        name: spec
        for name, spec in _DB_PROPERTIES.items()
        if name not in planned and name != "Title"  # the title column always exists
    })

    if not patch:
        return existing

    logger.info("Updating database schema: %s", ", ".join(sorted(patch)))
    try:
        resp = requests.patch(
            f"{NOTION_BASE_URL}/databases/{db_id}",
            headers=_headers(token),
            json={"properties": patch},
            timeout=30,
        )
        _check(resp, "schema update")
    except Exception as exc:
        logger.error(
            "Could not update the database schema (%s). Continuing with the "
            "columns that already exist; add the missing ones by hand in Notion "
            "if you want them: %s",
            exc, ", ".join(sorted(set(_DB_PROPERTIES) - existing)),
        )
        return existing

    # Re-read rather than assuming the patch applied exactly as sent — a
    # partially-accepted schema edit would otherwise poison every page write.
    return read_properties(db_id, token)


def ensure_database(
    parent_page_id: str,
    token: str,
    configured_db_id: str = "",
) -> Tuple[str, Set[str]]:
    """Resolve the database every run writes to, and report its column set.

    Returns ``(database_id, property_names)``. Callers need the second half
    because Notion rejects a page carrying a property the database lacks, and
    a schema edit is not guaranteed to succeed.

    See the module docstring for the resolution order.
    """
    state = _load_state()

    for db_id, origin in (
        (configured_db_id, "config.yaml"),
        (state.get("notion_database_id"), "state.json"),
    ):
        if db_id:
            logger.info("Reusing Notion database from %s: %s", origin, db_id)
            _remember_database(state, db_id)
            return db_id, _sync_schema(db_id, token)

    if found := _find_database_under_page(parent_page_id, token):
        logger.info("Found existing '%s' database under the parent page: %s",
                    _DB_TITLE, found)
        _remember_database(state, found)
        return found, _sync_schema(found, token)

    logger.info("Creating Notion database under parent page %s", parent_page_id)
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": _DB_TITLE}}],
        "icon": {"type": "emoji", "emoji": "📚"},
        "properties": _DB_PROPERTIES,
    }
    resp = requests.post(
        f"{NOTION_BASE_URL}/databases",
        headers=_headers(token),
        json=payload,
        timeout=30,
    )
    _check(resp, "database creation")
    db_id = resp.json()["id"]

    _remember_database(state, db_id)
    logger.info("Created database: %s", db_id)
    return db_id, set(_DB_PROPERTIES)


def _remember_database(state: dict, db_id: str) -> None:
    """Cache the resolved database ID so the next run skips the lookup."""
    if state.get("notion_database_id") == db_id:
        return
    state["notion_database_id"] = db_id
    _save_state(state)


# ── Page content (Korean notes as Notion blocks) ───────────────────────────────

def _text_block(text: str, block_type: str = "paragraph") -> dict:
    return {
        "type": block_type,
        block_type: {
            "rich_text": [{"type": "text", "text": {"content": text[:2000]}}]
        },
    }


def _heading2(text: str) -> dict:
    return _text_block(text, "heading_2")


def _build_page_content(paper: Paper) -> List[dict]:
    """Build Notion block children for the Korean note.

    Papers get four sections; news gets three — "방법" is a paper concept and an
    empty heading on a news page reads as a bug.
    """
    note: Optional[ResearchNote] = paper.research_note
    is_news = paper.content_type == "news"

    blocks: List[dict] = []

    # Header line. News never goes through LLM relevance scoring, so printing
    # "관련도 점수: 0/10" on a news page would be reporting a score that was
    # never computed — it gets its own signals instead.
    if is_news:
        signals = [f"📰 {paper.venue}"]
        if paper.points is not None:
            signals.append(f"👍 {paper.points} points")
        if paper.published_at:
            signals.append(f"🕐 {paper.published_at[:10]}")
        blocks.append(_heading2("  ·  ".join(signals)))
    else:
        blocks.append(_heading2(f"📊 관련도 점수: {paper.relevance_score:.0f}/10"))

    if paper.url:
        blocks.append(_text_block(f"🔗 {paper.url}"))

    # Section 1: 한 줄 요약
    blocks.append(_heading2("한 줄 요약"))
    blocks.append(_text_block(note.one_line_summary if note else ""))

    # Section 2: 핵심 기여 / 핵심 내용
    blocks.append(_heading2("핵심 내용" if is_news else "핵심 기여"))
    if note and note.key_contributions:
        for i, contrib in enumerate(note.key_contributions[:3], 1):
            blocks.append(
                {
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": f"{i}. {contrib}"[:2000]}}]
                    },
                }
            )
    else:
        blocks.append(_text_block("(내용 없음)"))

    # Section 3: 방법 — papers only
    if not is_news:
        blocks.append(_heading2("방법"))
        blocks.append(_text_block(note.method if note else ""))

    # Final section: 내 연구와의 연결점
    blocks.append(_heading2("내 연구와의 연결점"))
    blocks.append(_text_block(note.relevance_to_profile if note else ""))

    # Divider + metadata
    blocks.append({"type": "divider", "divider": {}})
    meta = [f"출처: {' + '.join(paper.source)}"]
    if paper.authors:
        meta.append(f"저자: {', '.join(paper.authors[:5])}")
    if paper.published_at and not is_news:
        # News already carries its date in the header line above.
        meta.append(f"발행일: {paper.published_at[:10]}")
    meta.append(f"수집일: {paper.collection_date}")
    blocks.append(_text_block(" | ".join(meta)))

    return blocks


# ── Page creation ──────────────────────────────────────────────────────────────

def create_page(
    paper: Paper,
    db_id: str,
    token: str,
    known_properties: Optional[Set[str]] = None,
) -> str:
    """Create a Notion page for a paper or news item. Returns the new page ID.

    *known_properties* is the database's actual column set. Notion rejects the
    whole page if it carries a property the database does not have, so a column
    that could not be created must not be written — one missing column would
    otherwise cost every page in the run.
    """
    tags = [{"name": kw} for kw in paper.matched_keywords[:10]]

    is_news = paper.content_type == "news"

    properties: dict = {
        "Title": {
            "title": [{"text": {"content": paper.title[:2000]}}]
        },
        "Type": {"select": {"name": "뉴스" if is_news else "논문"}},
        "Venue": {"select": {"name": paper.venue[:100]}},
        "Status": {"select": {"name": paper.venue_status[:100]}},
        # News is not LLM-scored. Writing 0 would sort every story below every
        # paper and read as "judged irrelevant"; an empty cell is the truth.
        "Score": {"number": None if is_news else paper.relevance_score},
        "Tags": {"multi_select": tags},
        "Collected": {"date": {"start": paper.collection_date}},
    }
    if paper.published_at:
        # Date-only: sources disagree on whether they give a timestamp, and a
        # column mixing "2026-08-14" with "2026-08-14 09:31" reads as a bug.
        properties["Published"] = {"date": {"start": paper.published_at[:10]}}
    if paper.url:
        properties["URL"] = {"url": paper.url}

    if known_properties is not None:
        dropped = set(properties) - known_properties
        if dropped:
            logger.warning("Database has no %s column(s) — writing without them",
                           ", ".join(sorted(dropped)))
        properties = {k: v for k, v in properties.items() if k in known_properties}

    children = _build_page_content(paper)

    payload = {
        "parent": {"database_id": db_id},
        "properties": properties,
        "children": children,
    }

    resp = requests.post(
        f"{NOTION_BASE_URL}/pages",
        headers=_headers(token),
        json=payload,
        timeout=30,
    )
    _check(resp, "page creation")
    page_id: str = resp.json()["id"]
    logger.info("Created Notion page: %s (%s)", page_id, paper.title[:60])
    return page_id


# ── Page update (batch venue mode) ────────────────────────────────────────────

def update_venue(page_id: str, venue: str, token: str) -> None:
    """Stamp an accepted venue onto an existing page.

    Status moves with it — leaving it at "preprint" would hand the same page
    back to the next batch run.
    """
    payload = {
        "properties": {
            "Venue": {"select": {"name": venue[:100]}},
            "Status": {"select": {"name": "accepted"}},
        }
    }
    resp = requests.patch(
        f"{NOTION_BASE_URL}/pages/{page_id}",
        headers=_headers(token),
        json=payload,
        timeout=30,
    )
    _check(resp, "venue update")
    logger.info("Updated venue to '%s' for page %s", venue, page_id)


# ── Database query (for batch mode) ───────────────────────────────────────────

def query_preprint_pages(db_id: str, token: str) -> List[dict]:
    """Return all pages in the DB still marked as preprints.

    Filters on Status, not on the venue name. Notion's ``select`` filter has no
    ``contains`` condition — only ``equals`` — so the previous
    ``Venue contains "arXiv"`` was rejected outright, and it would have missed
    the repositories that are not arXiv (Zenodo, OSF, institutional archives)
    even if it had worked.
    """
    results = []
    has_more = True
    cursor: Optional[str] = None

    while has_more:
        payload: dict = {
            "filter": {
                "property": "Status",
                "select": {"equals": "preprint"},
            },
            "page_size": 100,
        }
        if cursor:
            payload["start_cursor"] = cursor

        resp = requests.post(
            f"{NOTION_BASE_URL}/databases/{db_id}/query",
            headers=_headers(token),
            json=payload,
            timeout=30,
        )
        _check(resp, "database query")
        data = resp.json()
        results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")

    return results
