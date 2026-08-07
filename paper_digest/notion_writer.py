"""Notion integration: auto-create the database and write/update pages.

The database is auto-created on the first run under the configured parent page.
The database ID is persisted in ``state.json`` so subsequent runs can find it
without searching.

DB schema:
    Title       (title)
    Venue       (select)
    Score       (number)
    Tags        (multi_select)
    Date        (date)
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

def ensure_database(parent_page_id: str, token: str) -> str:
    """Return the Notion database ID, creating the DB if it doesn't exist yet."""
    state = _load_state()
    if db_id := state.get("notion_database_id"):
        logger.info("Reusing existing Notion database: %s", db_id)
        return db_id

    logger.info("Creating Notion database under parent page %s", parent_page_id)
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": _DB_TITLE}}],
        "icon": {"type": "emoji", "emoji": "📚"},
        "properties": {
            "Title": {"title": {}},
            "Venue": {"select": {}},
            "Score": {"number": {"format": "number"}},
            "Tags": {"multi_select": {}},
            "Date": {"date": {}},
        },
    }
    resp = requests.post(
        f"{NOTION_BASE_URL}/databases",
        headers=_headers(token),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    db_id = resp.json()["id"]

    state["notion_database_id"] = db_id
    _save_state(state)
    logger.info("Created database: %s", db_id)
    return db_id


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
    """Build Notion block children representing the Korean research note."""
    note: Optional[ResearchNote] = paper.research_note
    score = paper.relevance_score

    blocks: List[dict] = []

    # Score header
    blocks.append(_heading2(f"📊 관련도 점수: {score:.0f}/10"))

    # Section 1: 한 줄 요약
    blocks.append(_heading2("한 줄 요약"))
    blocks.append(_text_block(note.one_line_summary if note else ""))

    # Section 2: 핵심 기여
    blocks.append(_heading2("핵심 기여"))
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

    # Section 3: 방법
    blocks.append(_heading2("방법"))
    blocks.append(_text_block(note.method if note else ""))

    # Section 4: 내 연구와의 연결점
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
    """Create a Notion page for a paper. Returns the new page ID."""
    tags = [{"name": kw} for kw in paper.matched_keywords[:10]]

    properties: dict = {
        "Title": {
            "title": [{"text": {"content": paper.title[:2000]}}]
        },
        "Venue": {"select": {"name": paper.venue[:100]}},
        "Score": {"number": paper.relevance_score},
        "Tags": {"multi_select": tags},
        "Date": {"date": {"start": paper.collection_date}},
    }

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
