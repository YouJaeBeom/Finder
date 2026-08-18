"""An in-memory stand-in for the Notion REST API.

Written to replace per-test ``MagicMock`` wiring. The lab structure touches six
endpoints in one run — page probe, child listing, database read, database
create, page create, database query — and hand-mocking that per test file
produced mocks that agreed with each other but not with Notion. The specific bug
that motivated this: a mock returning the same children payload for every parent
made "each member gets their own database" pass while the code created one
database and wrote everyone into it.

This fake keeps real state instead, so a test can assert on what a run actually
wrote and to which database.

Faithful in the ways that matter here:

* a page or database created under a parent shows up in that parent's children
* ``child_database`` and ``child_page`` blocks are distinct types
* a database rejects nothing, but only reports the properties it actually has
* ``query`` paginates, and returns rows in the shape ``notion_query`` reads
* trashed pages and databases still resolve by ID but report ``in_trash``
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Dict, List, Optional

from paper_digest.notion_api import NOTION_BASE_URL

QUERY_PAGE_SIZE = 100


class FakeResponse:
    def __init__(self, status_code: int, payload: Optional[dict] = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers: Dict[str, str] = {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} error", response=self)


class FakeNotion:
    """A Notion workspace held in memory.

    Patch it in with :meth:`install`, which redirects the ``requests`` verbs on
    :mod:`paper_digest.notion_api` — the single module every Notion call in the
    tool goes through.
    """

    def __init__(self, root_page_id: str = "root-page"):
        self.root_page_id = root_page_id
        self.pages: Dict[str, dict] = {root_page_id: {"title": "root"}}
        self.databases: Dict[str, dict] = {}
        self.children: Dict[str, List[dict]] = defaultdict(list)
        self.rows: Dict[str, List[dict]] = defaultdict(list)
        self.trashed: set = set()
        self.calls: List[tuple] = []
        self._counter = 0

    # ── helpers for tests ─────────────────────────────────────────────────────

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def database_named(self, title: str, under: Optional[str] = None) -> Optional[str]:
        """The ID of a database with this title, optionally under a given parent."""
        for db_id, db in self.databases.items():
            if db["title"] == title and (under is None or db["parent"] == under):
                return db_id
        return None

    def page_named(self, title: str, under: Optional[str] = None) -> Optional[str]:
        for page_id, page in self.pages.items():
            if page.get("title") == title and (
                under is None or page.get("parent") == under
            ):
                return page_id
        return None

    def titles_in(self, db_id: str) -> List[str]:
        """The Title cell of every row in a database, in insertion order."""
        return [
            "".join(p.get("text", {}).get("content", "")
                    for p in row["properties"].get("Title", {}).get("title", []))
            for row in self.rows[db_id]
        ]

    def install(self, monkeypatch) -> "FakeNotion":
        import paper_digest.notion_api as api

        monkeypatch.setattr(api.requests, "get", self._get)
        monkeypatch.setattr(api.requests, "post", self._post)
        monkeypatch.setattr(api.requests, "patch", self._patch)
        # The throttle is real time; tests do not need to spend it.
        monkeypatch.setattr(api, "MIN_REQUEST_INTERVAL", 0.0)
        return self

    # ── the API surface ───────────────────────────────────────────────────────

    def _path(self, url: str) -> str:
        return url[len(NOTION_BASE_URL):] if url.startswith(NOTION_BASE_URL) else url

    def _get(self, url, headers=None, params=None, timeout=None, **kw):
        path = self._path(url)
        self.calls.append(("GET", path))

        if path.startswith("/pages/"):
            page_id = path.split("/")[2]
            if page_id not in self.pages:
                return FakeResponse(404, {"message": "Could not find page"})
            return FakeResponse(200, {"id": page_id,
                                      "in_trash": page_id in self.trashed})

        if path.startswith("/blocks/") and path.endswith("/children"):
            parent = path.split("/")[2]
            return FakeResponse(200, {
                "results": list(self.children.get(parent, [])),
                "has_more": False,
                "next_cursor": None,
            })

        if path.startswith("/databases/"):
            db_id = path.split("/")[2]
            if db_id not in self.databases:
                return FakeResponse(404, {"message": "Could not find database"})
            db = self.databases[db_id]
            return FakeResponse(200, {
                "id": db_id,
                "properties": {name: {} for name in db["properties"]},
                "in_trash": db_id in self.trashed,
            })

        return FakeResponse(404, {"message": f"unhandled GET {path}"})

    def _post(self, url, headers=None, json=None, timeout=None, **kw):
        path = self._path(url)
        body = json or {}
        self.calls.append(("POST", path))

        if path == "/databases":
            parent = body["parent"]["page_id"]
            title = body["title"][0]["text"]["content"]
            db_id = self._next_id("db")
            self.databases[db_id] = {
                "parent": parent,
                "title": title,
                "properties": set(body.get("properties", {})),
            }
            self.children[parent].append({
                "id": db_id, "type": "child_database",
                "child_database": {"title": title},
            })
            return FakeResponse(200, {"id": db_id})

        if path == "/pages":
            parent = body["parent"]
            if "database_id" in parent:
                db_id = parent["database_id"]
                if db_id not in self.databases:
                    return FakeResponse(404, {"message": "Could not find database"})
                unknown = set(body["properties"]) - self.databases[db_id]["properties"]
                if unknown:
                    # What Notion really does, and the reason create_page filters
                    # against the database's actual column set.
                    return FakeResponse(400, {
                        "message": f"{sorted(unknown)} is not a property that exists"
                    })
                row_id = self._next_id("row")
                self.rows[db_id].append({"id": row_id,
                                         "properties": body["properties"]})
                return FakeResponse(200, {"id": row_id})

            page_parent = parent["page_id"]
            title = body["properties"]["title"]["title"][0]["text"]["content"]
            page_id = self._next_id("page")
            self.pages[page_id] = {"parent": page_parent, "title": title}
            self.children[page_parent].append({
                "id": page_id, "type": "child_page", "child_page": {"title": title},
            })
            return FakeResponse(200, {"id": page_id})

        if path.startswith("/databases/") and path.endswith("/query"):
            db_id = path.split("/")[2]
            if db_id not in self.databases:
                return FakeResponse(404, {"message": "Could not find database"})
            rows = self.rows[db_id]
            start = int(body.get("start_cursor") or 0)
            page = rows[start:start + QUERY_PAGE_SIZE]
            end = start + len(page)
            return FakeResponse(200, {
                "results": [{"id": r["id"], "properties": _as_read(r["properties"])}
                            for r in page],
                "has_more": end < len(rows),
                "next_cursor": str(end) if end < len(rows) else None,
            })

        return FakeResponse(404, {"message": f"unhandled POST {path}"})

    def _patch(self, url, headers=None, json=None, timeout=None, **kw):
        path = self._path(url)
        body = json or {}
        self.calls.append(("PATCH", path))

        if path.startswith("/databases/"):
            db_id = path.split("/")[2]
            if db_id not in self.databases:
                return FakeResponse(404, {"message": "Could not find database"})
            self.databases[db_id]["properties"] |= set(body.get("properties", {}))
            return FakeResponse(200, {"id": db_id})

        return FakeResponse(404, {"message": f"unhandled PATCH {path}"})


def _as_read(written: dict) -> dict:
    """Convert a written property payload into the shape a read returns.

    Notion accepts ``{"text": {"content": …}}`` on write and answers with
    ``plain_text`` on read. ``notion_query`` reads ``plain_text``, so a fake that
    echoed the write shape back would let a broken index pass.
    """
    out = dict(written)
    if title := written.get("Title", {}).get("title"):
        out["Title"] = {
            "title": [{"plain_text": part.get("text", {}).get("content", "")}
                      for part in title]
        }
    return out
