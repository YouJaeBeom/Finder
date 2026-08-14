"""Database resolution — every run must land in the same Notion database.

The failure this guards against is specific and was live before these tests:
GitHub Actions checks out a clean tree, so state.json never survives between
scheduled runs, and ensure_database created a brand-new database every Monday.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from paper_digest.models import Paper, PaperIdentifiers
from paper_digest.notion_writer import (
    _DB_PROPERTIES,
    _DB_TITLE,
    create_page,
    ensure_database,
)

# Derived from the schema itself, so adding a column can't silently leave these
# tests asserting against a shape the code no longer writes.
FULL_SCHEMA = {"properties": {name: {} for name in _DB_PROPERTIES}}


def _resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def _children(*databases: tuple[str, str], has_more: bool = False, cursor=None) -> dict:
    """Build a blocks/children payload containing the given (id, title) databases."""
    return {
        "results": [
            {"id": db_id, "type": "child_database", "child_database": {"title": title}}
            for db_id, title in databases
        ],
        "has_more": has_more,
        "next_cursor": cursor,
    }


@pytest.fixture(autouse=True)
def _tmp_cwd(tmp_path, monkeypatch):
    """state.json is written CWD-relative; keep it out of the repo."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestDatabaseResolution:
    def test_configured_id_wins_and_creates_nothing(self):
        with (
            patch("paper_digest.notion_writer.requests.get", return_value=_resp(FULL_SCHEMA)),
            patch("paper_digest.notion_writer.requests.post") as post,
        ):
            db_id, _ = ensure_database("parent", "tok", configured_db_id="pinned-db")

        assert db_id == "pinned-db"
        post.assert_not_called()

    def test_cached_state_is_reused(self, _tmp_cwd):
        Path("state.json").write_text(json.dumps({"notion_database_id": "cached-db"}))

        with (
            patch("paper_digest.notion_writer.requests.get", return_value=_resp(FULL_SCHEMA)),
            patch("paper_digest.notion_writer.requests.post") as post,
        ):
            db_id, _ = ensure_database("parent", "tok")

        assert db_id == "cached-db"
        post.assert_not_called()

    def test_existing_database_under_parent_is_found_not_duplicated(self, _tmp_cwd):
        """The CI case: no state.json on disk, but the database already exists."""
        def fake_get(url, headers=None, params=None, timeout=None):
            if "/blocks/" in url:
                return _resp(_children(("found-db", _DB_TITLE)))
            return _resp(FULL_SCHEMA)

        with (
            patch("paper_digest.notion_writer.requests.get", side_effect=fake_get),
            patch("paper_digest.notion_writer.requests.post") as post,
        ):
            db_id, _ = ensure_database("parent", "tok")

        assert db_id == "found-db"
        post.assert_not_called()
        # Cached so the next run on this machine skips the lookup entirely.
        assert json.loads(Path("state.json").read_text())["notion_database_id"] == "found-db"

    def test_unrelated_database_under_parent_is_ignored(self):
        def fake_get(url, headers=None, params=None, timeout=None):
            if "/blocks/" in url:
                return _resp(_children(("someone-elses-db", "Reading list")))
            return _resp(FULL_SCHEMA)

        with (
            patch("paper_digest.notion_writer.requests.get", side_effect=fake_get),
            patch("paper_digest.notion_writer.requests.post",
                  return_value=_resp({"id": "new-db"})) as post,
        ):
            assert ensure_database("parent", "tok")[0] == "new-db"
        post.assert_called_once()

    def test_lookup_paginates_before_giving_up(self):
        pages = [
            _resp(_children(("x", "Other"), has_more=True, cursor="c1")),
            _resp(_children(("found-db", _DB_TITLE))),
        ]

        def fake_get(url, headers=None, params=None, timeout=None):
            if "/blocks/" in url:
                return pages.pop(0)
            return _resp(FULL_SCHEMA)

        with (
            patch("paper_digest.notion_writer.requests.get", side_effect=fake_get),
            patch("paper_digest.notion_writer.requests.post") as post,
        ):
            assert ensure_database("parent", "tok")[0] == "found-db"
        post.assert_not_called()

    def test_first_run_creates_and_caches(self, _tmp_cwd):
        with (
            patch("paper_digest.notion_writer.requests.get",
                  return_value=_resp(_children())),
            patch("paper_digest.notion_writer.requests.post",
                  return_value=_resp({"id": "brand-new-db"})) as post,
        ):
            assert ensure_database("parent", "tok")[0] == "brand-new-db"

        post.assert_called_once()
        assert json.loads(Path("state.json").read_text())["notion_database_id"] == "brand-new-db"

    def test_lookup_failure_raises_instead_of_duplicating(self):
        """A transient 500 must not be read as 'no database exists yet'."""
        with (
            patch("paper_digest.notion_writer.requests.get",
                  side_effect=requests.ConnectionError("boom")),
            patch("paper_digest.notion_writer.requests.post") as post,
        ):
            with pytest.raises(RuntimeError, match="Could not list children"):
                ensure_database("parent", "tok")

        post.assert_not_called()


class TestSchemaTopUp:
    def _sync(self, schema: dict):
        """Run ensure_database against a database with the given schema."""
        with (
            patch("paper_digest.notion_writer.requests.get", return_value=_resp(schema)),
            patch("paper_digest.notion_writer.requests.patch",
                  return_value=_resp({})) as patch_req,
        ):
            ensure_database("parent", "tok", configured_db_id="existing-db")
        return patch_req

    def test_missing_columns_are_added_to_an_older_database(self):
        """A database from before Type/URL existed would 400 on every write."""
        older = dict(FULL_SCHEMA["properties"])
        del older["Type"], older["URL"]
        patch_req = self._sync({"properties": older})

        patch_req.assert_called_once()
        assert set(patch_req.call_args.kwargs["json"]["properties"]) == {"Type", "URL"}

    def test_legacy_date_column_is_renamed_not_duplicated(self):
        """Every value ever written to "Date" was a collection date.

        Renaming keeps those rows correct; adding "Collected" alongside would
        leave the real dates stranded in a column the tool no longer writes.
        """
        patch_req = self._sync({"properties": {
            "Title": {}, "Type": {}, "Venue": {}, "Score": {},
            "Tags": {}, "Date": {}, "URL": {},
        }})

        props = patch_req.call_args.kwargs["json"]["properties"]
        assert props["Date"] == {"name": "Collected"}
        assert "Collected" not in props, "renamed column must not also be created"
        assert "Published" in props, "the new column is still added"

    def test_a_rejected_schema_edit_does_not_kill_the_run(self):
        """Notion accepts some schema edits and refuses others.

        Losing a whole week of papers because one column could not be added is
        wildly disproportionate — the run continues with what exists.
        """
        older = dict(FULL_SCHEMA["properties"])
        del older["Status"]

        failing = MagicMock()
        failing.raise_for_status.side_effect = requests.HTTPError("400")
        failing.json.return_value = {"message": "select cannot be updated"}

        with (
            patch("paper_digest.notion_writer.requests.get",
                  return_value=_resp({"properties": older})),
            patch("paper_digest.notion_writer.requests.patch", return_value=failing),
        ):
            db_id, properties = ensure_database("parent", "tok",
                                                configured_db_id="db")

        assert db_id == "db"
        assert "Status" not in properties, "a column that was refused is not claimed"
        assert "Title" in properties, "the columns that do exist are still usable"

    def test_the_schema_is_re_read_rather_than_assumed(self):
        """A partially-accepted edit would otherwise poison every page write."""
        after = dict(FULL_SCHEMA["properties"])
        del after["Status"]
        reads = [_resp({"properties": {"Title": {}}}), _resp({"properties": after})]

        with (
            patch("paper_digest.notion_writer.requests.get",
                  side_effect=lambda *a, **k: reads.pop(0)),
            patch("paper_digest.notion_writer.requests.patch", return_value=_resp({})),
        ):
            _, properties = ensure_database("parent", "tok", configured_db_id="db")

        assert "Status" not in properties
        assert "Venue" in properties

    def test_complete_schema_is_left_alone(self):
        with (
            patch("paper_digest.notion_writer.requests.get", return_value=_resp(FULL_SCHEMA)),
            patch("paper_digest.notion_writer.requests.patch") as patch_req,
        ):
            ensure_database("parent", "tok", configured_db_id="current-db")

        patch_req.assert_not_called()


class TestWritingAgainstAPartialSchema:
    """Notion rejects the whole page if it carries an unknown property, so a
    column that could not be created must not be written."""

    def _paper(self) -> Paper:
        return Paper(
            identifiers=PaperIdentifiers(arxiv_id="2408.00001"),
            title="A Paper", abstract="...", venue="ACL", venue_status="published",
            collection_date="2026-08-15", source=["arxiv"],
            url="https://arxiv.org/abs/2408.00001", published_at="2026-08-12",
        )

    def _create(self, known):
        with patch("paper_digest.notion_writer.requests.post",
                   return_value=_resp({"id": "page-1"})) as post:
            create_page(self._paper(), "db", "tok", known_properties=known)
        return post.call_args.kwargs["json"]["properties"]

    def test_missing_columns_are_dropped_not_sent(self):
        known = set(_DB_PROPERTIES) - {"Status", "Published"}
        written = self._create(known)

        assert "Status" not in written and "Published" not in written
        assert written["Title"], "the page is still created"
        assert written["Venue"] == {"select": {"name": "ACL"}}

    def test_a_full_schema_gets_everything(self):
        written = self._create(set(_DB_PROPERTIES))
        assert {"Title", "Type", "Venue", "Status", "Tags",
                "Collected", "Published", "URL"} <= set(written)

    def test_no_schema_given_means_send_everything(self):
        """Callers that don't know the schema keep the old behaviour."""
        assert "Status" in self._create(None)


class TestDeletedDatabase:
    """Deleting a database in Notion moves it to the trash, and the API still
    reads and writes it. A cached ID pointing there fails in the worst way: the
    run reports success while every page lands somewhere invisible."""

    def _trashed(self) -> MagicMock:
        payload = dict(FULL_SCHEMA)
        payload["in_trash"] = True
        return _resp(payload)

    def test_a_trashed_database_is_not_reused(self, _tmp_cwd):
        Path("state.json").write_text(json.dumps({"notion_database_id": "trashed-db"}))

        def fake_get(url, headers=None, params=None, timeout=None):
            if "/blocks/" in url:
                return _resp(_children())  # nothing live under the parent either
            return self._trashed()

        with (
            patch("paper_digest.notion_writer.requests.get", side_effect=fake_get),
            patch("paper_digest.notion_writer.requests.post",
                  return_value=_resp({"id": "fresh-db"})),
        ):
            db_id, _ = ensure_database("parent", "tok")

        assert db_id == "fresh-db", "a trashed database must not be written to"
        assert json.loads(Path("state.json").read_text())["notion_database_id"] == "fresh-db"

    def test_a_deleted_database_falls_back_to_the_one_under_the_page(self, _tmp_cwd):
        Path("state.json").write_text(json.dumps({"notion_database_id": "gone-db"}))
        gone = MagicMock(status_code=404)

        def fake_get(url, headers=None, params=None, timeout=None):
            if "/blocks/" in url:
                return _resp(_children(("live-db", _DB_TITLE)))
            if url.endswith("gone-db"):
                return gone
            return _resp(FULL_SCHEMA)

        with (
            patch("paper_digest.notion_writer.requests.get", side_effect=fake_get),
            patch("paper_digest.notion_writer.requests.post") as post,
        ):
            db_id, _ = ensure_database("parent", "tok")

        assert db_id == "live-db"
        post.assert_not_called()

    def test_a_live_database_is_still_reused(self):
        live = dict(FULL_SCHEMA)
        live["in_trash"] = False
        with (
            patch("paper_digest.notion_writer.requests.get", return_value=_resp(live)),
            patch("paper_digest.notion_writer.requests.post") as post,
        ):
            db_id, _ = ensure_database("parent", "tok", configured_db_id="live-db")

        assert db_id == "live-db"
        post.assert_not_called()


class TestSummaryColumn:
    """The one-liner belongs in a column, so the table is readable without
    opening every page."""

    def _paper(self, note=None) -> Paper:
        from paper_digest.models import ResearchNote
        return Paper(
            identifiers=PaperIdentifiers(), title="A Paper", abstract="...",
            venue="ACL", collection_date="2026-08-15",
            research_note=ResearchNote(note, ["a"], "m", "r") if note else None,
        )

    def _write(self, paper) -> dict:
        with patch("paper_digest.notion_writer.requests.post",
                   return_value=_resp({"id": "p1"})) as post:
            create_page(paper, "db", "tok")
        return post.call_args.kwargs["json"]["properties"]

    def test_the_one_line_summary_lands_in_the_column(self):
        written = self._write(self._paper("정치적 편향을 다국어로 측정한다."))
        assert written["Summary"]["rich_text"][0]["text"]["content"] == (
            "정치적 편향을 다국어로 측정한다.")

    def test_no_note_means_no_summary_property(self):
        assert "Summary" not in self._write(self._paper())

    def test_an_overlong_summary_is_truncated_to_notions_limit(self):
        written = self._write(self._paper("가" * 3000))
        assert len(written["Summary"]["rich_text"][0]["text"]["content"]) == 2000
