"""The Notion structure: a news database on the main page, one per member.

The failures these guard against are all "it worked, but somewhere else":

* GitHub Actions checks out a clean tree, so state.json never survives between
  scheduled runs. Without resolution by title, every Monday created a second copy
  of everything.
* Deleting a database in Notion only trashes it; the API keeps reading and
  writing it happily, so a cached ID pointing at the trash produces runs that
  report success while every page lands somewhere invisible.
* Notion rejects an entire page that carries one property the database lacks, so
  a column that could not be created must not be written.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paper_digest.models import Paper, PaperIdentifiers, ResearchNote
from paper_digest.notion_writer import (
    NEWS_DB_TITLE,
    NEWS_PROPERTIES,
    PAPER_PROPERTIES,
    PAPERS_DB_TITLE,
    create_page,
    ensure_member_space,
    ensure_news_database,
)
from tests.conftest import PARENT_PAGE_ID, make_paper
from tests.notion_fake import FakeNotion, FakeResponse

TOKEN = "tok"
ROOT = PARENT_PAGE_ID


@pytest.fixture(autouse=True)
def _tmp_cwd(tmp_path, monkeypatch):
    """state.json is written CWD-relative; keep it out of the repo."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture()
def notion(monkeypatch) -> FakeNotion:
    return FakeNotion(ROOT).install(monkeypatch)


class TestNewsDatabase:
    def test_created_on_the_main_page(self, notion):
        db_id, props = ensure_news_database(ROOT, TOKEN)

        assert notion.databases[db_id]["parent"] == ROOT
        assert notion.databases[db_id]["title"] == NEWS_DB_TITLE
        assert props == set(NEWS_PROPERTIES)

    def test_a_second_call_reuses_it(self, notion):
        first, _ = ensure_news_database(ROOT, TOKEN)
        second, _ = ensure_news_database(ROOT, TOKEN)

        assert first == second
        assert len(notion.databases) == 1

    def test_it_is_found_by_title_when_state_is_lost(self, notion):
        first, _ = ensure_news_database(ROOT, TOKEN)
        Path("state.json").unlink()

        second, _ = ensure_news_database(ROOT, TOKEN)
        assert second == first, "a lost state.json must not duplicate the database"

    def test_a_trashed_database_is_replaced_not_reused(self, notion):
        first, _ = ensure_news_database(ROOT, TOKEN)
        notion.trashed.add(first)
        # Also drop it from the children listing, as Notion does for trash.
        notion.children[ROOT] = [c for c in notion.children[ROOT]
                                 if c["id"] != first]

        second, _ = ensure_news_database(ROOT, TOKEN)
        assert second != first

    def test_it_carries_no_paper_only_columns(self, notion):
        _, props = ensure_news_database(ROOT, TOKEN)
        # News never goes through relevance scoring and has no review state, so
        # an empty Score or Status column would read as a bug rather than as
        # "not applicable".
        assert {"Score", "Status", "Tags", "Venue"}.isdisjoint(props)
        assert {"Source", "Points"} <= props


class TestMemberSpace:
    def test_a_page_and_a_database_inside_it(self, notion):
        space = ensure_member_space(ROOT, "jaebeom", "유재범", TOKEN)

        assert notion.pages[space.page_id]["parent"] == ROOT
        assert notion.pages[space.page_id]["title"] == "유재범"
        assert notion.databases[space.database_id]["parent"] == space.page_id
        assert notion.databases[space.database_id]["title"] == PAPERS_DB_TITLE
        assert space.properties == set(PAPER_PROPERTIES)

    def test_each_member_gets_a_separate_database(self, notion):
        a = ensure_member_space(ROOT, "a", "가", TOKEN)
        b = ensure_member_space(ROOT, "b", "나", TOKEN)

        assert a.page_id != b.page_id
        assert a.database_id != b.database_id
        assert len(notion.databases) == 2

    def test_a_second_run_reuses_both(self, notion):
        first = ensure_member_space(ROOT, "a", "가", TOKEN)
        second = ensure_member_space(ROOT, "a", "가", TOKEN)

        assert (first.page_id, first.database_id) == (second.page_id,
                                                     second.database_id)
        assert len(notion.pages) == 2   # root + the member page
        assert len(notion.databases) == 1

    def test_found_by_title_when_state_is_lost(self, notion):
        first = ensure_member_space(ROOT, "a", "가", TOKEN)
        Path("state.json").unlink()

        second = ensure_member_space(ROOT, "a", "가", TOKEN)
        assert (second.page_id, second.database_id) == (first.page_id,
                                                       first.database_id)

    def test_a_trashed_member_page_is_replaced(self, notion):
        first = ensure_member_space(ROOT, "a", "가", TOKEN)
        notion.trashed.add(first.page_id)
        notion.children[ROOT] = [c for c in notion.children[ROOT]
                                 if c["id"] != first.page_id]

        second = ensure_member_space(ROOT, "a", "가", TOKEN)
        assert second.page_id != first.page_id

    def test_state_records_both_ids_per_member(self, notion):
        ensure_member_space(ROOT, "a", "가", TOKEN)
        ensure_member_space(ROOT, "b", "나", TOKEN)

        state = json.loads(Path("state.json").read_text(encoding="utf-8"))
        assert set(state["members"]) == {"a", "b"}
        assert set(state["members"]["a"]) == {"page_id", "database_id"}

    def test_missing_columns_are_added_to_an_existing_database(self, notion):
        space = ensure_member_space(ROOT, "a", "가", TOKEN)
        # Simulate a database created by an earlier version of the schema.
        notion.databases[space.database_id]["properties"] = {"Title", "Venue"}

        again = ensure_member_space(ROOT, "a", "가", TOKEN)
        assert again.properties == set(PAPER_PROPERTIES)


class TestCreatePage:
    def _space(self, notion):
        return ensure_member_space(ROOT, "a", "가", TOKEN)

    def test_a_paper_writes_the_paper_columns(self, notion):
        space = self._space(notion)
        paper = make_paper(doi="10.1/x", score=8.0)
        paper.research_note = ResearchNote(
            one_line_summary="한 줄", key_contributions=["a", "b", "c"],
            method="방법", relevance_to_profile="연결점",
        )
        paper.published_at = "2026-08-01"
        paper.url = "https://doi.org/10.1/x"

        create_page(paper, space.database_id, TOKEN, space.properties)

        row = notion.rows[space.database_id][0]["properties"]
        assert row["Score"]["number"] == 8.0
        assert row["Kind"]["select"]["name"] == "conference"
        assert row["Venue"]["select"]["name"] == "ACL"
        assert row["Summary"]["rich_text"][0]["text"]["content"] == "한 줄"
        assert "Source" not in row and "Points" not in row

    def test_a_news_item_writes_the_news_columns(self, notion):
        news_db, props = ensure_news_database(ROOT, TOKEN)
        item = Paper(
            identifiers=PaperIdentifiers(url="https://e.com/1"),
            title="OpenAI ships something",
            abstract="summary",
            venue="Hacker News",
            content_type="news",
            collection_date="2026-08-19",
            source=["hackernews"],
            url="https://e.com/1",
            points=321,
        )
        create_page(item, news_db, TOKEN, props)

        row = notion.rows[news_db][0]["properties"]
        assert row["Source"]["select"]["name"] == "Hacker News"
        assert row["Points"]["number"] == 321
        assert "Score" not in row and "Kind" not in row

    def test_a_column_the_database_lacks_is_dropped_not_sent(self, notion):
        """Notion rejects the whole page for one unknown property.

        The fake enforces that too, so if the filter regressed this test would
        fail on the write rather than on an assertion.
        """
        space = self._space(notion)
        notion.databases[space.database_id]["properties"] = {"Title", "Venue"}

        page_id = create_page(make_paper(doi="10.1/y"), space.database_id, TOKEN,
                              known_properties={"Title", "Venue"})

        assert page_id
        assert set(notion.rows[space.database_id][0]["properties"]) == {"Title",
                                                                       "Venue"}

    def test_an_empty_collection_date_is_omitted(self, notion):
        """Notion rejects a date property whose start is an empty string."""
        space = self._space(notion)
        paper = make_paper(doi="10.1/z")
        paper.collection_date = ""

        create_page(paper, space.database_id, TOKEN, space.properties)
        assert "Collected" not in notion.rows[space.database_id][0]["properties"]


class TestInlineLayout:
    """Databases have to render as a table inside the page they live on.

    A full-page database shows up as a single link, so a member opening their
    own page sees "📚 논문" and has to click again to reach anything. The digest
    is meant to be glanceable, and that click is the difference.
    """

    def test_a_new_database_is_created_inline(self, fake_notion):
        db_id, _ = ensure_news_database("root-page", "tok")
        assert fake_notion.databases[db_id]["is_inline"] is True

    def test_a_members_paper_database_is_created_inline(self, fake_notion):
        space = ensure_member_space("root-page", "jaebeom", "유재범", "tok")
        assert fake_notion.databases[space.database_id]["is_inline"] is True

    def test_a_full_page_database_is_converted_on_the_next_run(self, fake_notion):
        """Workspaces built before this was the default repair themselves.

        The toggle belongs to whoever owns the block, so a member cannot fix
        their own page. Converting on resolve means an existing workspace
        converges on the current layout with no migration step.
        """
        db_id, _ = ensure_news_database("root-page", "tok")
        fake_notion.databases[db_id]["is_inline"] = False

        again, _ = ensure_news_database("root-page", "tok")

        assert again == db_id, "it must convert the database, not make a new one"
        assert fake_notion.databases[db_id]["is_inline"] is True

    def test_an_already_inline_database_is_not_patched_again(self, fake_notion):
        """Every run resolves every database; a needless PATCH each time is waste.

        Notion allows about three requests a second across the whole integration,
        and that budget is shared by every member's page writes.
        """
        db_id, _ = ensure_news_database("root-page", "tok")
        before = sum(1 for verb, path in fake_notion.calls
                     if verb == "PATCH" and path == f"/databases/{db_id}")

        ensure_news_database("root-page", "tok")

        after = sum(1 for verb, path in fake_notion.calls
                    if verb == "PATCH" and path == f"/databases/{db_id}")
        assert after == before

    def test_a_failed_conversion_does_not_stop_the_run(self, fake_notion, monkeypatch):
        """Layout is cosmetic; it must never cost a run the pages it was writing."""
        db_id, _ = ensure_news_database("root-page", "tok")
        fake_notion.databases[db_id]["is_inline"] = False

        real_patch = fake_notion._patch

        def refuse(url, **kw):
            if url.endswith(f"/databases/{db_id}") and (kw.get("json") or {}).get("is_inline"):
                return FakeResponse(400, {"message": "nope"})
            return real_patch(url, **kw)

        monkeypatch.setattr("paper_digest.notion_api.requests.patch", refuse)

        again, props = ensure_news_database("root-page", "tok")
        assert again == db_id
        assert props, "the schema still has to come back"
