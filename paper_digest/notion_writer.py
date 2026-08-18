"""Notion structure: one news database on the main page, one database per member.

    📄 <parent page>                 ← notion_parent_page_id
    ├── 📰 IT 뉴스                    ← shared, written once per run
    ├── 📄 유재범                     ← member page
    │   └── 📚 논문                   ← that member's own database
    ├── 📄 …
    └── 📄 …

Every part of that is creatable through the API, which is the whole reason for
the shape. The obvious alternative — one shared database with a ``Member``
column and a filtered view per person — cannot be automated at all: Notion's API
has no endpoint for creating a view, so each new member would need an operator
working in the Notion UI. Giving people their own database moves that work into
code, and costs nothing, because a member's database is already exactly the view
they wanted.

Resolution is the same three steps for every page and every database:

    1. the ID cached in state.json
    2. a child of the parent carrying the right title
    3. create it

Step 2 is what makes GitHub Actions safe — the runner checks out a clean tree,
so state.json never survives between scheduled runs and without the lookup every
Monday would create a second copy of everything. It is also why losing
state.json is harmless: the titles find their way back.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .models import Paper, ResearchNote
from .notion_api import check, load_state, request, save_state

logger = logging.getLogger(__name__)

PAPERS_DB_TITLE = "📚 논문"
NEWS_DB_TITLE = "📰 IT 뉴스"

# Notion takes a database's column order from the property order at creation
# time and the API cannot reorder them afterwards (order lives in the view,
# which is not exposed). So both schemas are ordered for reading: what the item
# is, then what it says, then where it came from, with bookkeeping last.
PAPER_PROPERTIES: Dict[str, dict] = {
    "Title": {"title": {}},
    "Summary": {"rich_text": {}},   # the note's one-liner, read without opening
    "Venue": {"select": {}},        # ACL / SIGIR / TPAMI
    "Score": {"number": {"format": "number"}},   # this member's relevance
    "Kind": {"select": {}},         # conference | journal
    "Status": {"select": {}},       # published | accepted
    "Published": {"date": {}},      # the paper's own date — what you sort by
    "Tags": {"multi_select": {}},
    "URL": {"url": {}},
    "Collected": {"date": {}},      # the day a run fetched it
}

# News carries no Score, Status or Tags. It never goes through LLM relevance
# scoring (the sources are curated by construction), it has no review state, and
# an empty column reads as a bug rather than as "not applicable".
NEWS_PROPERTIES: Dict[str, dict] = {
    "Title": {"title": {}},
    "Summary": {"rich_text": {}},
    "Source": {"select": {}},       # Hacker News / TechCrunch / The Verge
    "Points": {"number": {"format": "number"}},  # HN score; empty for RSS
    "Published": {"date": {}},
    "URL": {"url": {}},
    "Collected": {"date": {}},
}


@dataclass(frozen=True)
class MemberSpace:
    """Where one member's digest lives in Notion."""

    page_id: str
    database_id: str
    properties: Set[str]


# ── Lookup ─────────────────────────────────────────────────────────────────────

def _find_child(
    parent_id: str,
    token: str,
    block_type: str,
    title: str,
) -> Optional[str]:
    """The ID of a child page or database under *parent_id* with this title.

    Raising rather than returning None on a lookup failure is deliberate:
    falling through to "create" after a transient error would silently produce a
    duplicate database, and duplicates are far harder to notice than a failed run.
    """
    cursor: Optional[str] = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor

        resp = request("get", f"/blocks/{parent_id}/children", token,
                       what=f"child lookup ({parent_id})", params=params)
        check(resp, f"child lookup ({parent_id})")

        data = resp.json()
        for block in data.get("results", []):
            if block.get("type") != block_type:
                continue
            if block[block_type].get("title") == title:
                return block["id"]

        if not data.get("has_more"):
            return None
        cursor = data.get("next_cursor")


def _fetch_database(db_id: str, token: str) -> Optional[dict]:
    """The database object, or None if it is gone or sitting in the trash.

    Deleting a database in Notion moves it to the trash; the API still reads and
    writes it happily. A cached ID pointing at a trashed database therefore fails
    in the worst possible way — runs report success while every page lands
    somewhere invisible.
    """
    resp = request("get", f"/databases/{db_id}", token, what="read database schema")
    if resp.status_code == 404:
        return None
    check(resp, "read database schema")

    data = resp.json()
    if data.get("in_trash") or data.get("archived"):
        return None
    return data


def read_properties(db_id: str, token: str) -> Set[str]:
    """The column names the database currently has."""
    return set((_fetch_database(db_id, token) or {}).get("properties", {}))


# ── Creation ───────────────────────────────────────────────────────────────────

def _sync_schema(
    db_id: str,
    token: str,
    existing: Set[str],
    schema: Dict[str, dict],
) -> Set[str]:
    """Add any columns *schema* has that the database is missing.

    Returns the columns that exist afterwards — callers write only those, since
    Notion rejects an entire page carrying a property the database lacks.

    A failure here is reported and swallowed rather than raised. Notion accepts
    some schema edits and refuses others, and losing a member's whole week
    because one column could not be added is wildly disproportionate.
    """
    missing = {
        name: spec
        for name, spec in schema.items()
        if name not in existing and name != "Title"  # the title column is implicit
    }
    if not missing:
        return existing

    logger.info("Adding columns to %s: %s", db_id, ", ".join(sorted(missing)))
    try:
        resp = request("patch", f"/databases/{db_id}", token,
                       what="schema update", json_body={"properties": missing})
        check(resp, "schema update")
    except Exception as exc:
        logger.error(
            "Could not update the database schema (%s). Continuing with the "
            "columns that already exist; add these by hand in Notion if you "
            "want them: %s", exc, ", ".join(sorted(missing)),
        )
        return existing

    # Re-read rather than assuming the patch applied exactly as sent — a
    # partially accepted schema edit would otherwise poison every page write.
    return read_properties(db_id, token)


def _create_database(
    parent_page_id: str,
    token: str,
    title: str,
    emoji: str,
    schema: Dict[str, dict],
) -> str:
    logger.info("Creating Notion database %r under page %s", title, parent_page_id)
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": title}}],
        "icon": {"type": "emoji", "emoji": emoji},
        "properties": schema,
    }
    resp = request("post", "/databases", token, what="database creation",
                   json_body=payload)
    check(resp, "database creation")
    return resp.json()["id"]


def _create_subpage(parent_page_id: str, token: str, title: str, emoji: str) -> str:
    logger.info("Creating Notion page %r under page %s", title, parent_page_id)
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": emoji},
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": title[:2000]}}]}
        },
    }
    resp = request("post", "/pages", token, what="member page creation",
                   json_body=payload)
    check(resp, "member page creation")
    return resp.json()["id"]


def _resolve_database(
    parent_page_id: str,
    token: str,
    title: str,
    emoji: str,
    schema: Dict[str, dict],
    cached_id: Optional[str],
) -> Tuple[str, Set[str]]:
    """Cache → lookup by title → create. See the module docstring."""
    if cached_id:
        if database := _fetch_database(cached_id, token):
            return cached_id, _sync_schema(
                cached_id, token, set(database.get("properties", {})), schema
            )
        logger.warning(
            "Cached database %s (%s) no longer exists in Notion — looking for a "
            "live one instead", cached_id, title,
        )

    if found := _find_child(parent_page_id, token, "child_database", title):
        logger.info("Reusing %r under %s: %s", title, parent_page_id, found)
        return found, _sync_schema(found, token,
                                   read_properties(found, token), schema)

    db_id = _create_database(parent_page_id, token, title, emoji, schema)
    return db_id, set(schema)


def ensure_news_database(parent_page_id: str, token: str) -> Tuple[str, Set[str]]:
    """The shared news database, sitting directly on the workspace main page.

    On the main page rather than inside a member's space because news is
    collected, selected and written exactly once per run for everyone — see
    :mod:`paper_digest.news_select`.
    """
    state = load_state()
    db_id, props = _resolve_database(
        parent_page_id, token, NEWS_DB_TITLE, "📰", NEWS_PROPERTIES,
        state.get("news_database_id"),
    )
    if state.get("news_database_id") != db_id:
        state["news_database_id"] = db_id
        save_state(state)
    return db_id, props


def ensure_member_space(
    parent_page_id: str,
    member_id: str,
    member_name: str,
    token: str,
) -> MemberSpace:
    """The member's own page and the paper database inside it, creating both.

    Resolved by title, so renaming a member in their YAML file creates a fresh
    page rather than renaming the old one — which is the honest behaviour, since
    the tool cannot tell a rename from a different person joining.
    """
    state = load_state()
    members = state.setdefault("members", {})
    entry = members.setdefault(member_id, {})

    page_id = entry.get("page_id")
    if page_id is None or not page_exists(page_id, token):
        page_id = (_find_child(parent_page_id, token, "child_page", member_name)
                   or _create_subpage(parent_page_id, token, member_name, "👤"))

    db_id, props = _resolve_database(
        page_id, token, PAPERS_DB_TITLE, "📚", PAPER_PROPERTIES,
        entry.get("database_id"),
    )

    if entry.get("page_id") != page_id or entry.get("database_id") != db_id:
        members[member_id] = {"page_id": page_id, "database_id": db_id}
        save_state(state)

    return MemberSpace(page_id=page_id, database_id=db_id, properties=props)


def page_exists(page_id: str, token: str) -> bool:
    """Whether a cached page ID still points at a live page.

    Same trap as a trashed database: Notion keeps serving a deleted page through
    the API, so a run would happily create databases inside the trash.
    """
    resp = request("get", f"/pages/{page_id}", token, what="page lookup")
    if resp.status_code == 404:
        return False
    check(resp, "page lookup")
    data = resp.json()
    return not (data.get("in_trash") or data.get("archived"))


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

    blocks.append(_heading2("한 줄 요약"))
    blocks.append(_text_block(note.one_line_summary if note else ""))

    blocks.append(_heading2("핵심 내용" if is_news else "핵심 기여"))
    if note and note.key_contributions:
        for i, contrib in enumerate(note.key_contributions[:3], 1):
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [
                        {"type": "text", "text": {"content": f"{i}. {contrib}"[:2000]}}
                    ]
                },
            })
    else:
        blocks.append(_text_block("(내용 없음)"))

    if not is_news:
        blocks.append(_heading2("방법"))
        blocks.append(_text_block(note.method if note else ""))

    blocks.append(_heading2("내 연구와의 연결점"))
    blocks.append(_text_block(note.relevance_to_profile if note else ""))

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

def _properties_for(paper: Paper) -> dict:
    """The database columns for one item, by content type.

    Papers and news live in different databases with different schemas, so this
    builds what each one's database actually has rather than one union shape
    with half the cells empty.
    """
    props: dict = {
        "Title": {"title": [{"text": {"content": paper.title[:2000]}}]},
    }

    if paper.content_type == "news":
        props["Source"] = {"select": {"name": paper.venue[:100]}}
        props["Points"] = {"number": paper.points}
    else:
        props["Venue"] = {"select": {"name": paper.venue[:100]}}
        props["Score"] = {"number": paper.relevance_score}
        props["Status"] = {"select": {"name": paper.venue_status[:100]}}
        props["Kind"] = {
            "select": {"name": (paper.source[0] if paper.source else "unknown")[:100]}
        }
        props["Tags"] = {
            "multi_select": [{"name": kw[:100]} for kw in paper.matched_keywords[:10]]
        }

    if paper.research_note and paper.research_note.one_line_summary:
        props["Summary"] = {
            "rich_text": [
                {"text": {"content": paper.research_note.one_line_summary[:2000]}}
            ]
        }
    if paper.published_at:
        # Date-only: sources disagree on whether they give a timestamp, and a
        # column mixing "2026-08-14" with "2026-08-14 09:31" reads as a bug.
        props["Published"] = {"date": {"start": paper.published_at[:10]}}
    if paper.url:
        props["URL"] = {"url": paper.url}
    if paper.collection_date:
        props["Collected"] = {"date": {"start": paper.collection_date}}

    return props


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
    properties = _properties_for(paper)

    if known_properties is not None:
        if dropped := set(properties) - known_properties:
            logger.warning("Database has no %s column(s) — writing without them",
                           ", ".join(sorted(dropped)))
        properties = {k: v for k, v in properties.items() if k in known_properties}

    payload = {
        "parent": {"database_id": db_id},
        "properties": properties,
        "children": _build_page_content(paper),
    }

    resp = request("post", "/pages", token, what="page creation", json_body=payload)
    check(resp, "page creation")
    page_id: str = resp.json()["id"]
    logger.info("Created Notion page: %s (%s)", page_id, paper.title[:60])
    return page_id
