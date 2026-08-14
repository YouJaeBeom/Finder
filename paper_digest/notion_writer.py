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
    Title  (title)      Type  (select: 논문 | 뉴스)
    Venue  (select)     Score (number)
    Tags   (multi_select)
    Date   (date)       URL   (url)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

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
    "Score": {"number": {"format": "number"}},
    "Tags": {"multi_select": {}},
    "Date": {"date": {}},
    "URL": {"url": {}},
}


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
            resp.raise_for_status()
        except requests.RequestException as exc:
            # Falling through to create is worse than failing loudly here: a
            # transient error would silently produce a duplicate database.
            raise RuntimeError(
                f"Could not list children of parent page {parent_page_id}: {exc}"
            ) from exc

        data = resp.json()
        for block in data.get("results", []):
            if block.get("type") != "child_database":
                continue
            if block["child_database"].get("title") == _DB_TITLE:
                return block["id"]

        if not data.get("has_more"):
            return None
        cursor = data.get("next_cursor")


def _add_missing_properties(db_id: str, token: str) -> None:
    """Add any schema property the database is missing.

    A database created by an older version of this tool predates the Type and
    URL columns; writing a page with an unknown property is a hard 400, so the
    schema is topped up rather than left to fail at write time.
    """
    resp = requests.get(
        f"{NOTION_BASE_URL}/databases/{db_id}", headers=_headers(token), timeout=30
    )
    resp.raise_for_status()
    existing = set(resp.json().get("properties", {}))

    missing = {
        name: spec
        for name, spec in _DB_PROPERTIES.items()
        if name not in existing and name != "Title"  # the title column is never missing
    }
    if not missing:
        return

    logger.info("Adding missing database properties: %s", ", ".join(sorted(missing)))
    resp = requests.patch(
        f"{NOTION_BASE_URL}/databases/{db_id}",
        headers=_headers(token),
        json={"properties": missing},
        timeout=30,
    )
    resp.raise_for_status()


def ensure_database(
    parent_page_id: str,
    token: str,
    configured_db_id: str = "",
) -> str:
    """Resolve the one database every run writes to, creating it only if needed.

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
            _add_missing_properties(db_id, token)
            return db_id

    if found := _find_database_under_page(parent_page_id, token):
        logger.info("Found existing '%s' database under the parent page: %s",
                    _DB_TITLE, found)
        _remember_database(state, found)
        _add_missing_properties(found, token)
        return found

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
    resp.raise_for_status()
    db_id = resp.json()["id"]

    _remember_database(state, db_id)
    logger.info("Created database: %s", db_id)
    return db_id


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
    source_str = " + ".join(paper.source)
    authors_str = ", ".join(paper.authors[:5]) if paper.authors else "Unknown"
    blocks.append(
        _text_block(f"출처: {source_str} | 저자: {authors_str}")
    )

    return blocks


# ── Page creation ──────────────────────────────────────────────────────────────

def create_page(paper: Paper, db_id: str, token: str) -> str:
    """Create a Notion page for a paper or news item. Returns the new page ID."""
    tags = [{"name": kw} for kw in paper.matched_keywords[:10]]

    is_news = paper.content_type == "news"

    properties: dict = {
        "Title": {
            "title": [{"text": {"content": paper.title[:2000]}}]
        },
        "Type": {"select": {"name": "뉴스" if is_news else "논문"}},
        "Venue": {"select": {"name": paper.venue[:100]}},
        # News is not LLM-scored. Writing 0 would sort every story below every
        # paper and read as "judged irrelevant"; an empty cell is the truth.
        "Score": {"number": None if is_news else paper.relevance_score},
        "Tags": {"multi_select": tags},
        "Date": {"date": {"start": paper.collection_date}},
    }
    if paper.url:
        properties["URL"] = {"url": paper.url}

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
    resp.raise_for_status()
    page_id: str = resp.json()["id"]
    logger.info("Created Notion page: %s (%s)", page_id, paper.title[:60])
    return page_id


# ── Page update (batch venue mode) ────────────────────────────────────────────

def update_venue(page_id: str, venue: str, token: str) -> None:
    """Update the Venue property of an existing Notion page."""
    payload = {
        "properties": {
            "Venue": {"select": {"name": venue[:100]}},
        }
    }
    resp = requests.patch(
        f"{NOTION_BASE_URL}/pages/{page_id}",
        headers=_headers(token),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("Updated venue to '%s' for page %s", venue, page_id)


# ── Database query (for batch mode) ───────────────────────────────────────────

def query_preprint_pages(db_id: str, token: str) -> List[dict]:
    """Return all pages in the DB that still have venue_status == preprint."""
    results = []
    has_more = True
    cursor: Optional[str] = None

    while has_more:
        payload: dict = {
            "filter": {
                "property": "Venue",
                "select": {"contains": "arXiv"},
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
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")

    return results
