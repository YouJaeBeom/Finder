"""One-off backfill over a long window.

The weekly run asks "what appeared since last week" — right forever after, but it
leaves the prior year unread. Backfill ranks that year in one pass per member and
keeps the best N by relevance.

In a lab that raises a second question the single-user version did not have: when
one person joins an established group, backfilling everyone again would bill the
whole lab's catch-up twice. Hence ``--member``.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from paper_digest.models import Paper, PaperIdentifiers, normalize_title
from tests.conftest import (
    PARENT_PAGE_RAW,
    venue_collector,
    write_lab_config,
    write_member,
)

KEYWORDS = ["political bias"]
PROFILE = "LLM political bias 연구"


def _papers(n: int) -> list:
    out = []
    for i in range(n):
        title = f"Political Bias in Language Models, study {i}"
        out.append(Paper(
            identifiers=PaperIdentifiers(doi=f"10.1/{i}",
                                         normalized_title=normalize_title(title)),
            title=title,
            abstract="We measure political bias across languages.",
            venue="ACL", venue_status="published",
            collection_date="2026-08-15", source=["conference"],
            url=f"https://doi.org/10.1/{i}",
        ))
    return out


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.complete.side_effect = lambda prompt, model, max_tokens=512, \
        system=None: (json.dumps([{"id": str(i), "score": 10 - i} for i in range(20)])
                      if max_tokens <= 512 else json.dumps({
                          "one_line_summary": "요약",
                          "key_contributions": ["a", "b", "c"],
                          "method": "방법", "relevance_to_profile": "연결점"}))
    return provider


@pytest.fixture()
def one_member(tmp_path, monkeypatch, fake_notion) -> str:
    """A single-member lab, so backfill assertions stay about the window."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    write_member(tmp_path / "members", "pol", name="유재범", top_n=30,
                 keywords=KEYWORDS, profile=PROFILE)
    return write_lab_config(tmp_path, members=(), parent=PARENT_PAGE_RAW)


def _run(config_path, collected, limit=3, days=365, sources="both", only=None):
    pages = []

    def create(paper, db_id, token, known=None):
        pages.append(paper)
        return f"page-{len(pages)}"

    from paper_digest.pipeline import run_backfill

    with patch("paper_digest.pipeline.collect_venue_papers",
               side_effect=venue_collector(conference=collected)), \
         patch("paper_digest.pipeline.create_provider", return_value=_provider()), \
         patch("paper_digest.pipeline.create_page", side_effect=create):
        code = run_backfill(config_path, days=days, limit=limit, sources=sources,
                            only=only)
    return code, pages


class TestBackfill:
    def test_only_the_top_n_are_written(self, one_member):
        code, pages = _run(one_member, _papers(12), limit=3)
        assert code == 0
        assert len(pages) == 3

    def test_written_papers_are_the_highest_scoring(self, one_member):
        _, pages = _run(one_member, _papers(12), limit=3)
        scores = [p.relevance_score for p in pages]
        assert scores == sorted(scores, reverse=True)
        assert min(scores) >= 8, "the top three of a 10-down-to-1 ranking"

    def test_the_limit_beats_the_members_own_top_n(self, one_member):
        """The member's file says 30; the backfill was told 3."""
        _, pages = _run(one_member, _papers(12), limit=3)
        assert len(pages) == 3

    def test_every_ranked_paper_is_cached_not_just_the_written_ones(self, one_member):
        """Otherwise the next weekly run re-ranks thousands it already paid for."""
        _run(one_member, _papers(12), limit=3)

        cache = Path("state/scored/pol.json")
        assert cache.exists(), "the scoring cache is per member, under state/scored"
        state = json.loads(cache.read_text(encoding="utf-8"))
        assert len(state["records"]) == 12

    def test_a_second_backfill_finds_nothing_new(self, one_member):
        _run(one_member, _papers(12), limit=3)
        code, pages = _run(one_member, _papers(12), limit=3)

        assert code == 0
        assert pages == [], "already-considered papers are not reconsidered"

    def test_the_window_is_the_one_asked_for(self, one_member):
        seen = {}

        def collect(venues, days_back=7, max_results=500,
                    source_label="conference", exact_venue_match=False):
            seen[source_label] = days_back
            return []

        from paper_digest.pipeline import run_backfill

        with patch("paper_digest.pipeline.collect_venue_papers", side_effect=collect), \
             patch("paper_digest.pipeline.create_provider",
                   return_value=_provider()):
            code = run_backfill(one_member, days=180, limit=3, sources="both")

        assert code == 0
        assert seen == {"conference": 180, "journal": 180}, (
            "the backfill window has to reach the collectors — the weekly "
            "days_back is not what a backfill asked for"
        )

    def test_an_empty_year_is_not_a_failure(self, one_member):
        code, pages = _run(one_member, [], limit=3)
        assert code == 0 and pages == []


class TestMemberScoping:
    @pytest.fixture()
    def three_members(self, tmp_path, monkeypatch, fake_notion) -> str:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("NOTION_TOKEN", "t")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        for member_id, name in (("pol", "유재범"), ("newbie", "신입"),
                                ("other", "다른사람")):
            write_member(tmp_path / "members", member_id, name=name, top_n=30,
                         keywords=KEYWORDS, profile=PROFILE)
        return write_lab_config(tmp_path, members=(), parent=PARENT_PAGE_RAW)

    def test_without_member_every_member_is_backfilled(self, three_members):
        code, pages = _run(three_members, _papers(6), limit=2)
        assert code == 0
        assert len(pages) == 6, "two papers each for three members"

    def test_with_member_only_that_person_is_backfilled(self, three_members):
        code, pages = _run(three_members, _papers(6), limit=2, only="newbie")
        assert code == 0
        assert len(pages) == 2

        report = json.loads(Path("run-report.json").read_text(encoding="utf-8"))
        assert [r["member_id"] for r in report["members"]] == ["newbie"]
        # Nobody else's cache was touched, so nobody else was billed.
        assert not Path("state/scored/pol.json").exists()

    def test_an_unknown_member_is_an_error(self, three_members):
        code, pages = _run(three_members, _papers(6), only="ghost")
        assert code == 1 and pages == []


class TestSourceSelection:
    """Conferences are the case that needs backfilling: proceedings drop once a
    year, so a digest set up in August has missed the spring. Journals publish
    steadily and the weekly run picks them up on its own."""

    def _labels_for(self, config_path, sources):
        collect = MagicMock(side_effect=venue_collector())
        from paper_digest.pipeline import run_backfill

        with patch("paper_digest.pipeline.collect_venue_papers", collect), \
             patch("paper_digest.pipeline.create_provider",
                   return_value=_provider()):
            code = run_backfill(config_path, days=365, limit=10, sources=sources)
        return code, [c.kwargs.get("source_label") for c in collect.call_args_list]

    def test_conferences_only_skips_the_journal_source(self, one_member):
        code, labels = self._labels_for(one_member, "conferences")
        assert code == 0 and labels == ["conference"]

    def test_journals_only_skips_the_conference_source(self, one_member):
        code, labels = self._labels_for(one_member, "journals")
        assert code == 0 and labels == ["journal"]

    def test_both_uses_both(self, one_member):
        code, labels = self._labels_for(one_member, "both")
        assert code == 0 and labels == ["conference", "journal"]

    def test_an_unknown_source_fails_instead_of_silently_collecting_nothing(
            self, one_member):
        code, labels = self._labels_for(one_member, "preprints")
        assert code == 1 and labels == []
