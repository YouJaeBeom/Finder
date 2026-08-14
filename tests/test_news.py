"""IT news collection, ranking, note shape and the news pipeline stage.

These tests call the news stage directly rather than going through run_weekly,
and never read run-report.json — the pipeline writes that file CWD-relative, so
report-based assertions are order-sensitive across tests.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from paper_digest.collectors.hackernews import _story_to_paper
from paper_digest.collectors.rss import _strip_html
from paper_digest.config import Config, NewsConfig
from paper_digest.dedup import DedupStore
from paper_digest.models import Paper, PaperIdentifiers, ResearchNote, normalize_title
from paper_digest.notes import generate_note
from paper_digest.pipeline import _run_news_stage
from paper_digest.ranking import _is_rankable


def _news_item(title: str, url: str, summary: str | None = None) -> Paper:
    return Paper(
        identifiers=PaperIdentifiers(
            arxiv_id=None, doi=None, normalized_title=normalize_title(title), url=url
        ),
        title=title,
        abstract=summary,
        venue="Hacker News",
        venue_status="published",
        collection_date="2026-08-15",
        source=["hackernews"],
        content_type="news",
        url=url,
    )


# ── Collectors ────────────────────────────────────────────────────────────────

class TestHackerNewsCollector:
    def test_story_becomes_news_item(self):
        paper = _story_to_paper(
            {"type": "story", "title": "Anthropic ships X",
             "url": "https://example.com/a", "by": "alice", "score": 300},
            "2026-08-15",
        )
        assert paper is not None
        assert paper.content_type == "news"
        assert paper.venue == "Hacker News"
        assert paper.identifiers.url == "https://example.com/a"
        # HN serves no article body; it must stay None rather than be invented.
        assert paper.abstract is None

    def test_text_post_without_url_is_skipped(self):
        """Ask HN / Show HN text posts have no article and no identity key."""
        assert _story_to_paper(
            {"type": "story", "title": "Ask HN: what do you use?", "by": "bob"},
            "2026-08-15",
        ) is None


class TestRSSCollector:
    def test_strip_html_unescapes_and_collapses(self):
        assert _strip_html("<p>Hello &amp;   <b>world</b></p>") == "Hello & world"

    def test_strip_html_handles_empty(self):
        assert _strip_html("") == ""


# ── Ranking ───────────────────────────────────────────────────────────────────

class TestNewsRankability:
    def test_news_is_rankable_on_title_alone(self):
        """HN has no article body, so requiring one would drop the whole source."""
        assert _is_rankable(_news_item("Some headline", "https://e.com/1")) is True

    def test_paper_still_requires_an_abstract(self):
        paper = Paper(identifiers=PaperIdentifiers(), title="T", abstract=None)
        assert _is_rankable(paper) is False


# ── Note shape ────────────────────────────────────────────────────────────────

class TestNewsNoteShape:
    def test_news_note_has_three_sections_not_four(self):
        provider = MagicMock()
        provider.complete.return_value = json.dumps({
            "one_line_summary": "요약",
            "key_contributions": ["a", "b", "c"],
            "relevance_to_profile": "연결점",
        })
        note = generate_note(
            _news_item("headline", "https://e.com/1"),
            Config(research_profile="LLM 정렬"),
            provider,
        )
        assert note.content_type == "news"
        assert note.expected_sections() == 3
        assert note.is_complete(), "news note must count as complete without 방법"
        assert note.method == ""

    def test_paper_note_still_expects_four(self):
        note = ResearchNote("s", ["a"], "method", "rel", content_type="paper")
        assert note.expected_sections() == 4
        assert note.is_complete()


# ── News pipeline stage ───────────────────────────────────────────────────────

class TestNewsStage:
    @pytest.fixture(autouse=True)
    def _cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "seen_ids.json").write_text("[]")

    def _cfg(self, **news_kw) -> Config:
        defaults = dict(enabled=True, hacker_news_enabled=True,
                        hacker_news_min_points=100, rss_feeds=[], top_n=3)
        defaults.update(news_kw)
        return Config(
            keywords=["AI", "LLM"],
            research_profile="LLM 정렬 연구",
            notion_token="tok",
            anthropic_api_key="key",
            news=NewsConfig(**defaults),
        )

    def _provider(self) -> MagicMock:
        provider = MagicMock()

        def complete(prompt, model, max_tokens=512, system=None):
            if max_tokens <= 512:  # ranking
                return json.dumps([{"id": str(i), "score": 9} for i in range(20)])
            return json.dumps({  # note
                "one_line_summary": "요약",
                "key_contributions": ["a", "b", "c"],
                "relevance_to_profile": "연결점",
            })

        provider.complete.side_effect = complete
        return provider

    def test_disabled_news_does_nothing(self):
        written = _run_news_stage(
            self._cfg(enabled=False), MagicMock(), DedupStore(), "db"
        )
        assert written == []

    def test_writes_matching_stories_to_notion(self):
        stories = [
            _news_item("New LLM benchmark released", "https://e.com/1"),
            _news_item("AI chip startup raises round", "https://e.com/2"),
        ]
        page = MagicMock()
        with (
            patch("paper_digest.pipeline.collect_hackernews_stories", return_value=stories),
            patch("paper_digest.pipeline.collect_rss_entries", return_value=[]),
            patch("paper_digest.pipeline.create_page", side_effect=["p1", "p2"]),
        ):
            written = _run_news_stage(self._cfg(), self._provider(), DedupStore(), "db")

        assert len(written) == 2
        assert all(item.content_type == "news" for item in written)
        assert [item.notion_page_id for item in written] == ["p1", "p2"]

    def test_stories_missing_every_keyword_are_dropped(self):
        stories = [_news_item("Sourdough starter tips", "https://e.com/bread")]
        with (
            patch("paper_digest.pipeline.collect_hackernews_stories", return_value=stories),
            patch("paper_digest.pipeline.collect_rss_entries", return_value=[]),
            patch("paper_digest.pipeline.create_page") as create,
        ):
            written = _run_news_stage(self._cfg(), self._provider(), DedupStore(), "db")

        assert written == []
        create.assert_not_called()

    def test_already_seen_url_is_not_rewritten(self):
        story = _news_item("New LLM benchmark released", "https://e.com/1")
        store = DedupStore()
        store.mark_seen(story)

        # Same link, different headline — the URL is the identity for news.
        repost = _news_item("LLM benchmark, now on the front page", "https://e.com/1")
        with (
            patch("paper_digest.pipeline.collect_hackernews_stories", return_value=[repost]),
            patch("paper_digest.pipeline.collect_rss_entries", return_value=[]),
            patch("paper_digest.pipeline.create_page") as create,
        ):
            written = _run_news_stage(self._cfg(), self._provider(), store, "db")

        assert written == []
        create.assert_not_called()

    def test_collector_failure_is_contained(self):
        """A dead feed must not fail a run whose papers already landed."""
        with (
            patch("paper_digest.pipeline.collect_hackernews_stories",
                  side_effect=RuntimeError("HN down")),
            patch("paper_digest.pipeline.collect_rss_entries", return_value=[]),
        ):
            assert _run_news_stage(self._cfg(), self._provider(), DedupStore(), "db") == []
