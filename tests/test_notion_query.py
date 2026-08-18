"""The truth layer: what a database already holds.

Written after the 2026-08-15 incident, where a backfill's ``git push`` lost to a
race and the local dedup state was discarded — 84 minutes of collection and a
ranking bill gone, and the file left claiming 21 papers against a database holding
far more. With ten members writing weekly that race stops being an accident.

So "has this member already received this paper?" is answered by asking their
database. Losing local state now costs a lookup, never a duplicate page.
"""
from __future__ import annotations

import pytest

from paper_digest.models import Paper, normalize_title
from paper_digest.notion_query import WrittenIndex, written_index
from paper_digest.notion_writer import create_page, ensure_member_space
from tests.conftest import PARENT_PAGE_ID, make_paper
from tests.notion_fake import QUERY_PAGE_SIZE, FakeNotion

TOKEN = "tok"
ROOT = PARENT_PAGE_ID


@pytest.fixture()
def space(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    notion = FakeNotion(ROOT).install(monkeypatch)
    return notion, ensure_member_space(ROOT, "a", "가", TOKEN)


def _paper(title: str, url: str = None) -> Paper:
    paper = make_paper(title=title, doi=None)
    paper.identifiers.normalized_title = normalize_title(title)
    paper.identifiers.url = url
    paper.url = url
    return paper


class TestIndexFromNotion:
    def test_an_empty_database_contains_nothing(self, space):
        _, member = space
        index = written_index(member.database_id, TOKEN)
        assert index.row_count == 0
        assert not index.contains(_paper("Anything"))

    def test_a_written_paper_is_found_again_by_title(self, space):
        notion, member = space
        paper = _paper("Political Bias in Language Models")
        create_page(paper, member.database_id, TOKEN, member.properties)

        index = written_index(member.database_id, TOKEN)
        assert index.row_count == 1
        assert index.contains(_paper("Political Bias in Language Models"))

    def test_the_match_survives_punctuation_and_case(self, space):
        notion, member = space
        create_page(_paper("Bias, Fairness & LLMs"), member.database_id, TOKEN,
                    member.properties)

        index = written_index(member.database_id, TOKEN)
        assert index.contains(_paper("bias fairness  llms"))

    def test_a_url_match_is_enough_when_the_headline_changed(self, space):
        """A DOI lives inside the URL, so matching the URL matches the DOI."""
        notion, member = space
        create_page(_paper("First headline", url="https://doi.org/10.1/x"),
                    member.database_id, TOKEN, member.properties)

        index = written_index(member.database_id, TOKEN)
        assert index.contains(_paper("Completely different headline",
                                     url="https://doi.org/10.1/x"))

    def test_a_different_paper_is_not_a_match(self, space):
        notion, member = space
        create_page(_paper("Paper one"), member.database_id, TOKEN,
                    member.properties)

        index = written_index(member.database_id, TOKEN)
        assert not index.contains(_paper("Paper two"))

    def test_one_members_database_says_nothing_about_another(self, space):
        notion, member = space
        other = ensure_member_space(ROOT, "b", "나", TOKEN)
        create_page(_paper("Shared interest"), member.database_id, TOKEN,
                    member.properties)

        assert written_index(member.database_id, TOKEN).row_count == 1
        assert written_index(other.database_id, TOKEN).row_count == 0

    def test_every_page_of_results_is_read(self, space):
        """A database past one page would otherwise look mostly empty."""
        notion, member = space
        total = QUERY_PAGE_SIZE + 7
        for i in range(total):
            create_page(_paper(f"Paper number {i}"), member.database_id, TOKEN,
                        member.properties)

        index = written_index(member.database_id, TOKEN)
        assert index.row_count == total
        assert index.contains(_paper(f"Paper number {total - 1}"))

    def test_a_failed_query_raises_rather_than_reading_as_empty(self, space):
        """An empty index reads as "nothing written" and duplicates everything."""
        notion, member = space
        with pytest.raises(RuntimeError, match="database query"):
            written_index("no-such-database", TOKEN)

    def test_a_title_split_across_fragments_is_reassembled(self, space):
        """Notion chunks rich text at formatting boundaries."""
        notion, member = space
        notion.rows[member.database_id].append({
            "id": "row-x",
            "properties": {"Title": {"title": [{"text": {"content": "Half one "}},
                                               {"text": {"content": "half two"}}]}},
        })
        index = written_index(member.database_id, TOKEN)
        assert index.contains(_paper("Half one half two"))


class TestWithinRun:
    def test_add_prevents_a_second_write_in_the_same_run(self):
        index = WrittenIndex()
        paper = _paper("Same story", url="https://e.com/1")
        assert not index.contains(paper)

        index.add(paper)
        # Same link, different headline — the URL is the identity for news.
        assert index.contains(_paper("Rewritten headline", url="https://e.com/1"))

    def test_a_paper_with_no_url_is_still_tracked_by_title(self):
        index = WrittenIndex()
        index.add(_paper("No link here"))
        assert index.contains(_paper("No link here"))

    def test_an_untitled_unlinked_item_is_never_a_false_positive(self):
        index = WrittenIndex()
        index.add(_paper(""))
        assert not index.contains(_paper(""))
